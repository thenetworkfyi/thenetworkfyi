"""Server-side double-opt-in introduction workflow.

The model may propose a pair, but only this module records authenticated consent
and sends the identity-revealing group email.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlmodel import col, select

from thenetwork.audit import audit_event
from thenetwork.db.models import IntroductionConsent, Person
from thenetwork.db.session import get_session
from thenetwork.email.outbound import send_group_introduction, send_reply

_TOKEN_RE = re.compile(
    r"\[intro:(?P<token>[0-9a-f]{8}-[0-9a-f-]{27,})\]",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"^\s*(?:[\"'([{]\s*)?(?P<action>yes|no|revoke)"
    r"(?:\s*,?\s*(?:please|thanks?|thank\s+you))?"
    r"\s*[.!?;:)\]}'\"]*\s*$",
    re.IGNORECASE,
)
CONSENT_CLARIFICATION_REPLY = (
    "I could not determine your response. Reply with YES to opt in, "
    "NO to decline, or REVOKE to withdraw consent."
)
CONSENT_ACKNOWLEDGMENT_REPLY = "Noted — waiting on the other party."
CONSENT_DECLINED_REPLY = "Noted — this introduction will not go ahead."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_pair(person_a_id: str, person_b_id: str) -> tuple[str, str]:
    if not person_a_id or not person_b_id or person_a_id == person_b_id:
        raise ValueError("an introduction requires two distinct person ids")
    return tuple(sorted((person_a_id, person_b_id)))  # type: ignore[return-value]


def _pair_record(session, person_a_id: str, person_b_id: str):
    low, high = canonical_pair(person_a_id, person_b_id)
    record = session.exec(
        select(IntroductionConsent).where(
            IntroductionConsent.person_a_id == low,
            IntroductionConsent.person_b_id == high,
        )
    ).first()
    return record if isinstance(record, IntroductionConsent) else None


def pair_is_suppressed(session, person_a_id: str, person_b_id: str) -> bool:
    """Return whether this pair has already been proposed or resolved."""
    return _pair_record(session, person_a_id, person_b_id) is not None


def _outstanding_request_count(session, person_id: str) -> int:
    """Count consent requests still awaiting a response for one person."""
    requests = session.exec(
        select(IntroductionConsent).where(
            (IntroductionConsent.person_a_id == person_id)
            | (IntroductionConsent.person_b_id == person_id),
            col(IntroductionConsent.status).in_(("proposed", "one_consented")),
        )
    ).all()
    return len(requests)


def _recent_request_count(session, person_id: str, *, since: datetime) -> int:
    """Count all consent requests delivered to one person within a window."""
    requests = session.exec(
        select(IntroductionConsent).where(
            (IntroductionConsent.person_a_id == person_id)
            | (IntroductionConsent.person_b_id == person_id),
            col(IntroductionConsent.created_at) >= since,
        )
    ).all()
    return len(requests)


def propose_pair(
    *,
    sender_person_id: str,
    other_person_id: str,
    sender_gist: str,
    other_gist: str,
    session_factory: Callable = get_session,
    trace_id: str | None = None,
    max_outstanding_requests_per_person: int = 3,
    max_requests_per_person_in_window: int = 3,
    request_window_seconds: int = 86_400,
    decline_cooldown_days: int = 90,
) -> dict[str, str | int]:
    """Create one proposal and send fixed, anonymous consent requests."""
    if not sender_person_id or not other_person_id:
        return {"status": "error", "reason": "invalid_person_id"}
    if sender_person_id == other_person_id:
        return {"status": "error", "reason": "self_introduction"}

    low, high = canonical_pair(sender_person_id, other_person_id)
    with session_factory() as session:
        existing = _pair_record(session, low, high)
        if existing is not None:
            if (
                existing.status == "declined"
                and existing.declined_at is not None
                and existing.declined_at <= _utcnow() - timedelta(days=decline_cooldown_days)
            ):
                existing.status = "proposed"
                existing.person_a_consented = False
                existing.person_b_consented = False
                existing.declined_at = None
                existing.reply_token = str(uuid.uuid4())
                existing.updated_at = _utcnow()
                session.add(existing)
                session.commit()
                session.refresh(existing)
                proposal = existing
            else:
                return {"status": "suppressed", "reason": existing.status}
        else:
            proposal = None

        if (
            max_requests_per_person_in_window > 0
            and request_window_seconds > 0
        ):
            since = _utcnow() - timedelta(seconds=request_window_seconds)
            for person_id in (low, high):
                if (
                    _recent_request_count(session, person_id, since=since)
                    >= max_requests_per_person_in_window
                ):
                    return {
                        "status": "deferred",
                        "reason": "recipient_consent_request_cap",
                        "limit": max_requests_per_person_in_window,
                    }

        if max_outstanding_requests_per_person > 0:
            for person_id in (low, high):
                if (
                    _outstanding_request_count(session, person_id)
                    >= max_outstanding_requests_per_person
                ):
                    return {
                        "status": "deferred",
                        "reason": "recipient_outstanding_request_cap",
                        "limit": max_outstanding_requests_per_person,
                    }

        sender = session.get(Person, sender_person_id)
        other = session.get(Person, other_person_id)
        if sender is None or other is None:
            return {"status": "error", "reason": "person_not_found"}

        if proposal is None:
            proposal = IntroductionConsent(person_a_id=low, person_b_id=high)
            session.add(proposal)
            session.commit()
            session.refresh(proposal)

        subject = f"Possible introduction [intro:{proposal.reply_token}]"
        token = f"[intro:{proposal.reply_token}]"
        sender_body = (
            "A possible match came up:\n\n"
            f"{other_gist}\n\n"
            "No name or contact details have been shared. Reply YES to opt in, "
            "or NO to decline. If you reply from another thread, include this "
            f"token in your reply: {token}"
        )
        other_body = (
            "A possible match came up:\n\n"
            f"{sender_gist}\n\n"
            "No name or contact details have been shared. Reply YES to opt in, "
            "or NO to decline. If you reply from another thread, include this "
            f"token in your reply: {token}"
        )
        send_reply(
            to_address=sender.email,
            subject=subject,
            body_text=sender_body,
            trace_id=trace_id,
        )
        send_reply(
            to_address=other.email,
            subject=subject,
            body_text=other_body,
            trace_id=trace_id,
        )
    audit_event(
        "introduction.consent_transition",
        action="propose",
        record_type="introduction_consent",
        outcome="success",
        consent_state="proposed",
    )
    return {"status": "proposed"}


def _reply_action(body: str) -> str | None:
    visible = _visible_reply_lines(body)
    if not visible:
        return None
    match = _ACTION_RE.fullmatch(visible[0])
    if match is None:
        return None
    return {"yes": "consent", "no": "decline", "revoke": "revoke"}[match.group("action").lower()]


def _visible_reply_lines(body: str) -> list[str]:
    """Return non-quoted, non-empty lines authored in this reply."""
    return [
        line.strip()
        for line in body.replace("\r", "").splitlines()
        if line.strip() and not line.lstrip().startswith(">")
    ]


def _reply_token(subject: str, body: str) -> str | None:
    """Find a consent token in the subject or a visible reply line."""
    match = _TOKEN_RE.search(subject)
    if match is not None:
        return match.group("token")
    for line in _visible_reply_lines(body):
        match = _TOKEN_RE.search(line)
        if match is not None:
            return match.group("token")
    return None


def _send_fixed_reply(
    *,
    to_address: str,
    subject: str,
    body_text: str,
    trace_id: str | None,
) -> None:
    send_reply(
        to_address=to_address,
        subject=f"Re: {subject}",
        body_text=body_text,
        include_footer=False,
        trace_id=trace_id,
    )


@dataclass(frozen=True)
class ConsentReplyResult:
    handled: bool
    outcome: str | None = None


def process_consent_reply(
    *,
    sender_person_id: str | None,
    sender_authenticated: bool,
    subject: str,
    body: str,
    session_factory: Callable = get_session,
    trace_id: str | None = None,
) -> ConsentReplyResult:
    """Consume a tokened consent reply before any untrusted text reaches the model."""
    token = _reply_token(subject, body)
    if token is None:
        return ConsentReplyResult(handled=False)

    action = _reply_action(body)
    if not sender_authenticated or sender_person_id is None:
        audit_event(
            "introduction.consent_transition",
            action=action or "clarify",
            record_type="introduction_consent",
            outcome="rejected_unauthenticated",
        )
        return ConsentReplyResult(handled=True, outcome="rejected")

    with session_factory() as session:
        proposal = session.exec(
            select(IntroductionConsent)
            .where(IntroductionConsent.reply_token == token)
            .with_for_update()
        ).first()
        if proposal is None or sender_person_id not in (
            proposal.person_a_id,
            proposal.person_b_id,
        ):
            audit_event(
                "introduction.consent_transition",
                action=action,
                record_type="introduction_consent",
                outcome="rejected_forbidden",
            )
            return ConsentReplyResult(handled=True, outcome="rejected")

        sender = session.get(Person, sender_person_id)
        if sender is None:
            raise RuntimeError("introduction participant no longer exists")

        if action is None:
            _send_fixed_reply(
                to_address=sender.email,
                subject=subject,
                body_text=CONSENT_CLARIFICATION_REPLY,
                trace_id=trace_id,
            )
            audit_event(
                "introduction.consent_transition",
                action="clarify",
                record_type="introduction_consent",
                outcome="success",
                consent_state=proposal.status,
            )
            return ConsentReplyResult(handled=True, outcome="clarification_sent")

        if proposal.status == "revoked":
            return ConsentReplyResult(handled=True, outcome=proposal.status)

        if action == "revoke":
            proposal.status = "revoked"
            proposal.updated_at = _utcnow()
            session.add(proposal)
            session.commit()
            state = "revoked"
        elif action == "decline":
            proposal.status = "declined"
            proposal.declined_at = _utcnow()
            proposal.updated_at = _utcnow()
            session.add(proposal)
            session.commit()
            state = "declined"
            _send_fixed_reply(
                to_address=sender.email, subject=subject,
                body_text=CONSENT_DECLINED_REPLY, trace_id=trace_id,
            )
        elif proposal.status == "introduced":
            return ConsentReplyResult(handled=True, outcome="introduced")
        else:
            if sender_person_id == proposal.person_a_id:
                proposal.person_a_consented = True
            else:
                proposal.person_b_consented = True
            proposal.updated_at = _utcnow()
            if proposal.person_a_consented and proposal.person_b_consented:
                person_a = session.get(Person, proposal.person_a_id)
                person_b = session.get(Person, proposal.person_b_id)
                if person_a is None or person_b is None:
                    raise RuntimeError("introduction participant no longer exists")
                send_group_introduction(
                    person_a_name=person_a.name,
                    person_a_email=person_a.email,
                    person_b_name=person_b.name,
                    person_b_email=person_b.email,
                    trace_id=trace_id,
                )
                proposal.status = "introduced"
            else:
                proposal.status = "one_consented"
            session.add(proposal)
            session.commit()
            state = proposal.status
            if state == "one_consented":
                _send_fixed_reply(
                    to_address=sender.email,
                    subject=subject,
                    body_text=CONSENT_ACKNOWLEDGMENT_REPLY,
                    trace_id=trace_id,
                )

    audit_event(
        "introduction.consent_transition",
        action=action,
        record_type="introduction_consent",
        outcome="success",
        consent_state=state,
    )
    return ConsentReplyResult(handled=True, outcome=state)
