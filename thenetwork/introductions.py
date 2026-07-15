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
from thenetwork.db.models import (
    IntroductionConsent,
    PendingIntroCandidate,
    Person,
    ProactiveSurface,
)
from thenetwork.db.session import get_session
from thenetwork.email.outbound import send_group_introduction, send_reply
from thenetwork.settings import get_settings

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
CONSENT_ALREADY_DECLINED_REPLY = (
    "This introduction has already been declined and will not go ahead."
)

_DIGEST_LABELS = ("A", "B", "C", "D", "E", "F")
_DIGEST_MAX_SIZE = 6
_DIGEST_TOKEN_RE = re.compile(
    r"\[digest:(?P<token>[0-9a-f]{8}-[0-9a-f-]{27,})\]",
    re.IGNORECASE,
)
_DIGEST_SELECTION_RE = re.compile(
    r"^\s*(?:[\"'([{]\s*)?"
    r"(?P<body>none|[a-d](?:\s*(?:,|and|&)\s*[a-d])*)"
    r"(?:\s*,?\s*(?:please|thanks?|thank\s+you))?"
    r"\s*[.!?;:)\]}'\"]*\s*$",
    re.IGNORECASE,
)
DIGEST_CLARIFICATION_REPLY = (
    "I could not determine your selection. Reply with the letter(s) of the "
    'candidates you would like to pursue (e.g. "A" or "A, C"), or NONE.'
)
DIGEST_NONE_SELECTED_REPLY = "Noted — no introductions will be sent from this digest."
DIGEST_ALREADY_RESOLVED_REPLY = (
    "This digest has already been resolved and no longer accepts a selection."
)


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
    return record


def pair_is_suppressed(
    session,
    person_a_id: str,
    person_b_id: str,
    *,
    decline_cooldown_days: int = 90,
) -> bool:
    """Return whether this pair is proposed, resolved, or still cooling down."""
    record = _pair_record(session, person_a_id, person_b_id)
    if record is None:
        return False
    return not (
        record.status == "declined"
        and record.declined_at is not None
        and record.declined_at <= _utcnow() - timedelta(days=decline_cooldown_days)
    )


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


def request_load(session, person_id: str, *, since: datetime) -> int:
    """Current outstanding/recent consent-request load for one person.

    Mirrors the two signals `propose_pair` enforces as hard caps at proposal
    time, but here it is read-only: callers use it to *prioritize* candidates
    (e.g. proactive-scan pacing), not to reject them. The caps themselves stay
    enforced only in `propose_pair`.
    """
    return max(
        _outstanding_request_count(session, person_id),
        _recent_request_count(session, person_id, since=since),
    )


def recently_surfaced_pairs(session, *, since: datetime) -> set[tuple[str, str]]:
    """Return opaque pairs already handed to a proactive agent recently."""
    records = session.exec(
        select(ProactiveSurface).where(col(ProactiveSurface.surfaced_at) >= since)
    ).all()
    return {
        canonical_pair(record.person_a_id, record.person_b_id) for record in records
    }


def mark_pairs_surfaced(
    session,
    pairs: set[tuple[str, str]],
    *,
    surfaced_at: datetime | None = None,
) -> None:
    """Durably mark opaque pairs as having been surfaced to a proactive agent."""
    if not pairs:
        return

    timestamp = surfaced_at or _utcnow()
    for person_a_id, person_b_id in pairs:
        low, high = canonical_pair(person_a_id, person_b_id)
        record = session.exec(
            select(ProactiveSurface).where(
                ProactiveSurface.person_a_id == low,
                ProactiveSurface.person_b_id == high,
            )
        ).first()
        if record is None:
            session.add(
                ProactiveSurface(
                    person_a_id=low,
                    person_b_id=high,
                    surfaced_at=timestamp,
                )
            )
        else:
            record.surfaced_at = timestamp
            session.add(record)
    session.commit()


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
    queue_on_cap: bool = False,
) -> dict[str, str | int]:
    """Create one proposal and send fixed, anonymous consent requests.

    `queue_on_cap` (proactive callers only): when a per-person request cap
    would otherwise silently drop this candidate, queue it for a digest
    instead - see `queue_intro_candidate`/`flush_pending_digests`.
    """
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
                and existing.declined_at
                <= _utcnow() - timedelta(days=decline_cooldown_days)
            ):
                proposal = existing
            else:
                return {"status": "suppressed", "reason": existing.status}
        else:
            proposal = None

        if max_requests_per_person_in_window > 0 and request_window_seconds > 0:
            # Unconditional: this bounds a recipient's own inbound volume within
            # the window regardless of counterpart freshness. A per-counterpart
            # freshness exemption here would let a stream of distinct
            # never-before-consented proposers each get a pass against the same
            # saturated recipient, defeating the cap entirely.
            since = _utcnow() - timedelta(seconds=request_window_seconds)
            for person_id in (low, high):
                if (
                    _recent_request_count(session, person_id, since=since)
                    >= max_requests_per_person_in_window
                ):
                    if queue_on_cap:
                        _queue_capped_candidate(
                            capped_person_id=person_id,
                            sender_person_id=sender_person_id,
                            sender_gist=sender_gist,
                            other_person_id=other_person_id,
                            other_gist=other_gist,
                            session_factory=session_factory,
                        )
                    return {
                        "status": "deferred",
                        "reason": "recipient_consent_request_cap",
                        "limit": max_requests_per_person_in_window,
                    }

        if max_outstanding_requests_per_person > 0:
            # Same reasoning: bounds simultaneously *open* (unresolved) requests
            # unconditionally, regardless of counterpart freshness.
            for person_id in (low, high):
                if (
                    _outstanding_request_count(session, person_id)
                    >= max_outstanding_requests_per_person
                ):
                    if queue_on_cap:
                        _queue_capped_candidate(
                            capped_person_id=person_id,
                            sender_person_id=sender_person_id,
                            sender_gist=sender_gist,
                            other_person_id=other_person_id,
                            other_gist=other_gist,
                            session_factory=session_factory,
                        )
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
        else:
            proposal.status = "proposed"
            proposal.person_a_consented = False
            proposal.person_b_consented = False
            proposal.declined_at = None
            proposal.reply_token = str(uuid.uuid4())
            proposal.updated_at = _utcnow()
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


def queue_intro_candidate(
    *,
    recipient_person_id: str,
    candidate_person_id: str,
    recipient_gist: str,
    candidate_gist: str,
    session_factory: Callable = get_session,
) -> dict[str, str]:
    """Queue a candidate for batched digest delivery instead of an immediate request.

    Used when a proactively-sourced proposal cannot send its own consent
    request right away because the recipient is already at their outstanding-
    or window-request cap (`propose_pair`'s `queue_on_cap`). Rather than drop
    the candidate, it is held here until `flush_pending_digests` batches it
    with any other queued candidates for the same recipient into one digest
    email.
    """
    if not recipient_person_id or not candidate_person_id:
        return {"status": "error", "reason": "invalid_person_id"}
    if recipient_person_id == candidate_person_id:
        return {"status": "error", "reason": "self_introduction"}

    with session_factory() as session:
        if pair_is_suppressed(session, recipient_person_id, candidate_person_id):
            return {"status": "suppressed"}

        existing = session.exec(
            select(PendingIntroCandidate).where(
                PendingIntroCandidate.recipient_person_id == recipient_person_id,
                PendingIntroCandidate.candidate_person_id == candidate_person_id,
            )
        ).first()
        if existing is not None:
            return {"status": "already_queued", "candidate_status": existing.status}

        row = PendingIntroCandidate(
            recipient_person_id=recipient_person_id,
            candidate_person_id=candidate_person_id,
            recipient_gist=recipient_gist,
            candidate_gist=candidate_gist,
            status="queued",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        candidate_id = row.id

    audit_event(
        "introduction.digest_transition",
        action="queue",
        record_type="pending_intro_candidate",
        outcome="success",
        consent_state="queued",
    )
    return {"status": "queued", "candidate_id": candidate_id}


def _queue_capped_candidate(
    *,
    capped_person_id: str,
    sender_person_id: str,
    sender_gist: str,
    other_person_id: str,
    other_gist: str,
    session_factory: Callable,
) -> None:
    """Queue whichever side of a capped proposal hit its request limit."""
    if capped_person_id == sender_person_id:
        recipient_gist, candidate_id, candidate_gist = (
            sender_gist,
            other_person_id,
            other_gist,
        )
    else:
        recipient_gist, candidate_id, candidate_gist = (
            other_gist,
            sender_person_id,
            sender_gist,
        )
    queue_intro_candidate(
        recipient_person_id=capped_person_id,
        candidate_person_id=candidate_id,
        recipient_gist=recipient_gist,
        candidate_gist=candidate_gist,
        session_factory=session_factory,
    )


def flush_pending_digests(
    *,
    session_factory: Callable = get_session,
    trace_id: str | None = None,
) -> dict[str, int]:
    """Batch each recipient's queued candidates into one digest email.

    Every recipient with at least one `queued` `PendingIntroCandidate` row
    gets exactly one digest listing up to `introduction_digest_size` (capped
    at `_DIGEST_MAX_SIZE`) candidates, oldest first; any further queued
    candidates for that recipient wait for the next flush. The digest body
    carries only gists and opaque labels (SEAL-safe) - no names, no
    addresses, no raw memory text.
    """
    s = get_settings()
    cap = max(1, min(s.introduction_digest_size, _DIGEST_MAX_SIZE))
    sent = 0

    with session_factory() as session:
        queued = session.exec(
            select(PendingIntroCandidate)
            .where(PendingIntroCandidate.status == "queued")
            .order_by(col(PendingIntroCandidate.created_at).asc())
        ).all()

        by_recipient: dict[str, list[PendingIntroCandidate]] = {}
        for row in queued:
            by_recipient.setdefault(row.recipient_person_id, []).append(row)

        for recipient_id, rows in by_recipient.items():
            batch = rows[:cap]
            recipient = session.get(Person, recipient_id)
            if recipient is None:
                continue

            token = str(uuid.uuid4())
            lines = []
            for row, label in zip(batch, _DIGEST_LABELS):
                lines.append(f"{label}. {row.candidate_gist}")
                row.status = "digested"
                row.digest_token = token
                row.label = label
                row.updated_at = _utcnow()
                session.add(row)
            session.commit()

            body = (
                "A few possible matches came up:\n\n"
                + "\n\n".join(lines)
                + "\n\nNo names or contact details have been shared. Reply with "
                'the letter(s) of the ones you would like to pursue (e.g. "A" '
                'or "A, C"), or NONE. If you reply from another thread, '
                f"include this token in your reply: [digest:{token}]"
            )
            send_reply(
                to_address=recipient.email,
                subject=f"Possible introductions [digest:{token}]",
                body_text=body,
                trace_id=trace_id,
            )
            sent += 1

    audit_event(
        "introduction.digest_transition",
        action="flush",
        record_type="pending_intro_candidate",
        outcome="success",
        consent_state="digested",
    )
    return {"digests_sent": sent}


def _reply_action(body: str) -> str | None:
    visible = _visible_reply_lines(body)
    if not visible:
        return None
    match = _ACTION_RE.fullmatch(visible[0])
    if match is None:
        return None
    return {"yes": "consent", "no": "decline", "revoke": "revoke"}[
        match.group("action").lower()
    ]


def _visible_reply_lines(body: str) -> list[str]:
    """Return non-quoted, non-empty lines authored in this reply."""
    return [
        line.strip()
        for line in body.replace("\r", "").splitlines()
        if line.strip() and not line.lstrip().startswith(">")
    ]


def _reply_remainder(body: str) -> str:
    """Sender-authored reply text beyond the decision line and consent tokens.

    This is what the consent path would otherwise discard: visible lines minus
    the matched decision word (if any) and any `[intro:...]` tokens.
    """
    lines = _visible_reply_lines(body)
    if lines and _ACTION_RE.fullmatch(lines[0]):
        lines = lines[1:]
    kept = []
    for line in lines:
        stripped = _TOKEN_RE.sub("", line).strip()
        if stripped:
            kept.append(stripped)
    return "\n".join(kept)


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
    # Authenticated-participant reply text beyond the decision and token, so
    # the worker can hand it to an agent run instead of discarding it. Always
    # empty on rejected outcomes: only text from a verified participant in the
    # pair may travel onward.
    remainder: str = ""


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

        remainder = _reply_remainder(body)

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
            return ConsentReplyResult(
                handled=True, outcome="clarification_sent", remainder=remainder
            )

        if proposal.status == "revoked":
            return ConsentReplyResult(
                handled=True, outcome=proposal.status, remainder=remainder
            )

        if proposal.status == "declined":
            _send_fixed_reply(
                to_address=sender.email,
                subject=subject,
                body_text=CONSENT_ALREADY_DECLINED_REPLY,
                trace_id=trace_id,
            )
            return ConsentReplyResult(
                handled=True, outcome=proposal.status, remainder=remainder
            )

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
                to_address=sender.email,
                subject=subject,
                body_text=CONSENT_DECLINED_REPLY,
                trace_id=trace_id,
            )
        elif proposal.status == "introduced":
            return ConsentReplyResult(
                handled=True, outcome="introduced", remainder=remainder
            )
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
    return ConsentReplyResult(handled=True, outcome=state, remainder=remainder)


def _digest_reply_token(subject: str, body: str) -> str | None:
    """Find a digest token in the subject or a visible reply line."""
    match = _DIGEST_TOKEN_RE.search(subject)
    if match is not None:
        return match.group("token")
    for line in _visible_reply_lines(body):
        match = _DIGEST_TOKEN_RE.search(line)
        if match is not None:
            return match.group("token")
    return None


def _digest_selection(body: str) -> set[str] | None:
    """Parse the recipient's letter selection (or NONE) from a digest reply."""
    visible = _visible_reply_lines(body)
    if not visible:
        return None
    match = _DIGEST_SELECTION_RE.fullmatch(visible[0])
    if match is None:
        return None
    text = match.group("body").lower()
    if text == "none":
        return set()
    return {letter.upper() for letter in re.findall(r"\b[a-d]\b", text)}


@dataclass(frozen=True)
class DigestReplyResult:
    handled: bool
    outcome: str | None = None
    remainder: str = ""


def process_digest_reply(
    *,
    sender_person_id: str | None,
    sender_authenticated: bool,
    subject: str,
    body: str,
    session_factory: Callable = get_session,
    trace_id: str | None = None,
) -> DigestReplyResult:
    """Consume a tokened digest selection reply before it reaches the model.

    Selected candidates get a normal `[intro:...]` consent request via
    `propose_pair` (server-side, no agent involvement - the selection itself
    is a deterministic reply, not a judgment call).
    """
    token = _digest_reply_token(subject, body)
    if token is None:
        return DigestReplyResult(handled=False)

    selection = _digest_selection(body)
    if not sender_authenticated or sender_person_id is None:
        audit_event(
            "introduction.digest_transition",
            action="select" if selection is not None else "clarify",
            record_type="pending_intro_candidate",
            outcome="rejected_unauthenticated",
        )
        return DigestReplyResult(handled=True, outcome="rejected")

    with session_factory() as session:
        all_rows = session.exec(
            select(PendingIntroCandidate)
            .where(PendingIntroCandidate.digest_token == token)
            .with_for_update()
        ).all()

        if not all_rows or all_rows[0].recipient_person_id != sender_person_id:
            audit_event(
                "introduction.digest_transition",
                action="select" if selection is not None else "clarify",
                record_type="pending_intro_candidate",
                outcome="rejected_forbidden",
            )
            return DigestReplyResult(handled=True, outcome="rejected")

        recipient = session.get(Person, sender_person_id)
        if recipient is None:
            raise RuntimeError("digest recipient no longer exists")

        remainder = _reply_remainder(body)
        rows = [row for row in all_rows if row.status == "digested"]

        if not rows:
            _send_fixed_reply(
                to_address=recipient.email,
                subject=subject,
                body_text=DIGEST_ALREADY_RESOLVED_REPLY,
                trace_id=trace_id,
            )
            return DigestReplyResult(
                handled=True, outcome="already_resolved", remainder=remainder
            )

        if selection is None:
            _send_fixed_reply(
                to_address=recipient.email,
                subject=subject,
                body_text=DIGEST_CLARIFICATION_REPLY,
                trace_id=trace_id,
            )
            audit_event(
                "introduction.digest_transition",
                action="clarify",
                record_type="pending_intro_candidate",
                outcome="success",
                consent_state="digested",
            )
            return DigestReplyResult(
                handled=True, outcome="clarification_sent", remainder=remainder
            )

        by_label = {row.label: row for row in rows}
        chosen_rows = [by_label[label] for label in selection if label in by_label]

        for row in rows:
            row.status = "selected" if row in chosen_rows else "not_selected"
            row.updated_at = _utcnow()
            session.add(row)
        session.commit()

        if not chosen_rows:
            _send_fixed_reply(
                to_address=recipient.email,
                subject=subject,
                body_text=DIGEST_NONE_SELECTED_REPLY,
                trace_id=trace_id,
            )
            audit_event(
                "introduction.digest_transition",
                action="select",
                record_type="pending_intro_candidate",
                outcome="success",
                consent_state="not_selected",
            )
            return DigestReplyResult(
                handled=True, outcome="none_selected", remainder=remainder
            )

        chosen_payload = [
            {
                "recipient_person_id": row.recipient_person_id,
                "candidate_person_id": row.candidate_person_id,
                "recipient_gist": row.recipient_gist,
                "candidate_gist": row.candidate_gist,
            }
            for row in chosen_rows
        ]

    s = get_settings()
    for payload in chosen_payload:
        propose_pair(
            sender_person_id=payload["recipient_person_id"],
            other_person_id=payload["candidate_person_id"],
            sender_gist=payload["recipient_gist"],
            other_gist=payload["candidate_gist"],
            session_factory=session_factory,
            trace_id=trace_id,
            max_outstanding_requests_per_person=(
                s.introduction_max_outstanding_requests_per_person
            ),
            max_requests_per_person_in_window=(
                s.introduction_max_requests_per_person_in_window
            ),
            request_window_seconds=s.introduction_request_window_seconds,
            decline_cooldown_days=s.consent_decline_cooldown_days,
        )

    audit_event(
        "introduction.digest_transition",
        action="select",
        record_type="pending_intro_candidate",
        outcome="success",
        consent_state="selected",
    )
    return DigestReplyResult(handled=True, outcome="selected", remainder=remainder)
