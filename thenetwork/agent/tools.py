"""pydantic-ai agent tools for The Network.

Security contracts (THE SEAL) are structurally enforced here:
- inspect_user_profile: full data only for sender's OWN profile; opaque for others
- search_candidates: opaque IDs + non-identifying rationale only
- dispatch_email: opaque recipient_user_id, address resolved server-side
- Role separation: untrusted body arrives as user-role, never touches system prompt
"""
from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext
from sqlmodel import select

from thenetwork.agent.deps import AgentDeps
from thenetwork.db.models import Profile
from thenetwork.db.session import get_session
from thenetwork.embed.embeddings import embed_profile, embed_text
from thenetwork.email.outbound import send_reply
from thenetwork.search.match import match_candidates


async def inspect_user_profile(ctx: RunContext[AgentDeps], user_id: str) -> dict[str, Any]:
    """Return profile data.

    Full PII returned only when user_id == the current sender's own profile.
    For all other users: opaque ID + non-identifying fields only (minimal disclosure).
    """
    deps = ctx.deps
    with get_session() as session:
        profile = session.get(Profile, user_id)

    if profile is None:
        return {"error": "not_found"}

    if user_id == deps.sender_user_id:
        # Own profile — full data
        return {
            "id": profile.id,
            "name": profile.name,
            "bio": profile.bio,
            "skills": profile.skills,
            "intent_description": profile.intent_description,
            "available_to_collaborate": profile.available_to_collaborate,
        }
    # Other user — opaque, no PII
    return {
        "id": profile.id,
        "skills": profile.skills,
        "intent_description": profile.intent_description,
        "available_to_collaborate": profile.available_to_collaborate,
    }


async def save_or_update_profile(
    ctx: RunContext[AgentDeps],
    name: str,
    bio: str,
    skills: list[str],
    intent_description: str,
    available_to_collaborate: bool = True,
) -> dict[str, str]:
    """Upsert the sender's own profile and recompute vectors atomically."""
    deps = ctx.deps
    identity_vec, intent_vec = await embed_profile(bio, intent_description)

    with get_session() as session:
        existing = session.exec(
            select(Profile).where(Profile.email == deps.sender_email)
        ).first()

        if existing:
            existing.name = name
            existing.bio = bio
            existing.skills = skills
            existing.intent_description = intent_description
            existing.available_to_collaborate = available_to_collaborate
            existing.identity_vector = identity_vec
            existing.intent_vector = intent_vec
            session.add(existing)
            profile_id = existing.id
        else:
            profile = Profile(
                name=name,
                email=deps.sender_email,
                bio=bio,
                skills=skills,
                intent_description=intent_description,
                available_to_collaborate=available_to_collaborate,
                identity_vector=identity_vec,
                intent_vector=intent_vec,
            )
            session.add(profile)
            session.flush()
            profile_id = profile.id

    return {"status": "ok", "user_id": profile_id}


async def search_candidates(
    ctx: RunContext[AgentDeps],
    intent_text: str,
    required_skills: list[str] | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Find matching profiles via vector search + graph proximity.

    Returns opaque IDs and non-identifying rationale only (minimal disclosure).
    Names, emails, and raw bios of other users are NEVER returned.
    """
    deps = ctx.deps
    if not deps.sender_user_id:
        return []

    query_vector = await embed_text(intent_text)
    results = await match_candidates(
        query_vector=query_vector,
        requester_id=deps.sender_user_id,
        required_skills=required_skills,
        top_k=top_k,
    )
    return [
        {
            "user_id": r.user_id,
            "similarity": round(r.similarity, 3),
            "mutual_connections": round(r.mutual_connections, 3),
            "combined_score": round(r.combined_score, 3),
            "skill_overlap": r.skill_overlap,
        }
        for r in results
    ]


async def dispatch_email(
    ctx: RunContext[AgentDeps],
    recipient_user_id: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> dict[str, str]:
    """Send an email to a user by opaque ID.

    The LLM supplies only the opaque internal ID; this function resolves the
    real address server-side. The LLM never handles or sees raw email addresses
    (capability-style confused-deputy fix).
    """
    with get_session() as session:
        profile = session.get(Profile, recipient_user_id)

    if profile is None:
        return {"status": "error", "reason": "recipient_not_found"}

    send_reply(
        to_address=profile.email,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )
    return {"status": "sent"}
