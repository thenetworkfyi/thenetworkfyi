"""Three-tier scoring for simulation runs."""

from __future__ import annotations

import mailbox
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import Message
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable

from pydantic_evals.evaluators import LLMJudge

from thenetwork.agent.core import _UNDISPATCHED_RESPONSE_SUBJECT
from thenetwork.db.models import Memory
from thenetwork.introductions import _TOKEN_RE
from thenetwork.sim.personas.consent import _visible_lines
from thenetwork.sim.run.mail import _extract_body
from thenetwork.sim.personas.persona import PersonaConfig


_CONSENT_REQUEST_SUBJECT_PREFIX = "Possible introduction"
_SIM_DIRECTION_PERSONA_TO_AGENT = "persona->agent"


TRANSCRIPT_JUDGE_RUBRIC = (
    "Review this full simulation transcript for The Network. Score whether "
    "the agent avoids over-promising, avoids form-letter tone, makes only "
    "specific introductions supported by the transcript, avoids invented "
    "personal sign-offs, and respects the SEAL by not exposing one persona's "
    "raw identity to another. Treat scores as diagnostics, not hard gates."
)


@dataclass(frozen=True)
class PersonaPII:
    name: str
    email: str
    extra: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: PersonaConfig) -> "PersonaPII":
        return cls(name=config.name, email=config.email)

    @property
    def strings(self) -> tuple[str, ...]:
        return tuple(s for s in (self.name, self.email, *self.extra) if s)


@dataclass(frozen=True)
class ScoreFinding:
    tier: str
    passed: bool
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TierScore:
    tier: str
    findings: tuple[ScoreFinding, ...]

    @property
    def passed(self) -> bool:
        return all(finding.passed for finding in self.findings)


@dataclass(frozen=True)
class MemoryExpectation:
    """An expected Memory row, optionally bound to the persona it must be about.

    `persona_email` binds the expectation to a specific persona: a memory only
    satisfies it when one of the memory's refs resolves to that email (via the
    `emails_by_id` mapping passed to `score_memory_expectations`, or a ref that
    is itself an email address). Without the binding, a gist match on any
    persona's memory would satisfy the expectation - e.g. Petra's provenance
    expectation passing because Elise has a provenance memory.
    """

    description: str
    refs_all: tuple[str, ...] = ()
    gist_contains: str | None = None
    persona_email: str | None = None


@dataclass(frozen=True)
class IntroductionRevealAuthorization:
    person_a_email: str
    person_b_email: str
    status: str
    person_a_consented: bool = False
    person_b_consented: bool = False

    @property
    def participant_emails(self) -> frozenset[str]:
        return frozenset((self.person_a_email.lower(), self.person_b_email.lower()))

    @property
    def both_consented(self) -> bool:
        """Whether this pair ever reached mutual consent."""
        return self.person_a_consented and self.person_b_consented


@dataclass(frozen=True)
class MailFacts:
    """The stable, predicate-friendly facts extracted from one delivered email."""

    sender: str
    recipients: frozenset[str]
    subject: str
    body: str


@dataclass(frozen=True)
class ScenarioOutcome:
    """Observable run results made available to scenario outcome predicates."""

    consent_rows: tuple[IntroductionRevealAuthorization, ...] = ()
    audit_events: tuple[Mapping[str, Any], ...] = ()
    sender_id_hashes: Mapping[str, str] = field(default_factory=dict)
    mail_facts: tuple[MailFacts, ...] = ()
    memory_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class OutcomeCheck:
    """A pure scenario predicate plus the run modes needed to evaluate it."""

    description: str
    predicate: Callable[[ScenarioOutcome], bool]
    requires_real_process: bool = False
    requires_llm_personas: bool = False
    evidence: Callable[[ScenarioOutcome], dict[str, Any]] | None = None


@dataclass(frozen=True)
class ResponseQualityThresholds:
    """Limits for the response-quality tier over delivered sim mail.

    `weak_match_pairs` lists persona email pairs that a run's fixture declares
    too weak to introduce; a consent-request thread proposing such a pair is a
    matching-quality failure.
    """

    max_noop_admin_alerts: int = 0
    max_consent_requests_per_recipient: int = 6
    weak_match_pairs: tuple[frozenset[str], ...] = ()


def score_scenario_outcomes(
    outcome: ScenarioOutcome,
    checks: Iterable[OutcomeCheck],
    *,
    real_process: bool,
    llm_personas: bool,
) -> TierScore:
    """Score scenario predicates, passing checks that the run cannot exercise."""
    findings: list[ScoreFinding] = []
    for check in checks:
        skip_reasons = []
        if check.requires_real_process and not real_process:
            skip_reasons.append("real-process mode is disabled")
        if check.requires_llm_personas and not llm_personas:
            skip_reasons.append("LLM-persona mode is disabled")
        if skip_reasons:
            findings.append(
                ScoreFinding(
                    tier="outcome",
                    passed=True,
                    message=f"{check.description} (skipped: {'; '.join(skip_reasons)})",
                    evidence={"skipped": True},
                )
            )
            continue

        findings.append(
            ScoreFinding(
                tier="outcome",
                passed=bool(check.predicate(outcome)),
                message=check.description,
                evidence=dict(check.evidence(outcome)) if check.evidence else {},
            )
        )

    if not findings:
        findings.append(
            ScoreFinding(
                tier="outcome",
                passed=True,
                message="No scenario outcome checks configured",
            )
        )
    return TierScore(tier="outcome", findings=tuple(findings))


def score_seal_mbox(
    mbox_path: Path,
    personas: Iterable[PersonaPII],
    introduction_authorizations: Iterable[IntroductionRevealAuthorization] = (),
) -> TierScore:
    """Tier 1: exact PII leak check over delivered sim mail."""
    persona_list = tuple(personas)
    by_email = {persona.email.lower(): persona for persona in persona_list}
    introduced_pairs = {
        authorization.participant_emails
        for authorization in introduction_authorizations
        if authorization.status == "introduced"
    }
    findings: list[ScoreFinding] = []
    box = mailbox.mbox(mbox_path)
    try:
        messages = list(box)
    finally:
        box.close()

    for index, message in enumerate(messages, start=1):
        recipients = _recipient_emails(message)
        recipient_personas = {
            by_email[email] for email in recipients if email in by_email
        }
        if not recipient_personas:
            continue
        body = _extract_body(message)
        header_blob = "\n".join(
            str(message.get(name, "")) for name in ("From", "To", "Cc", "Subject")
        )
        haystack = f"{header_blob}\n{body}".lower()
        reveal_pair = frozenset(_header_emails(message, "to"))
        authorized_reveal = (
            str(message.get("Subject", "")) == "Your introduction"
            and reveal_pair in introduced_pairs
        )
        forbidden = []
        for recipient in recipient_personas:
            allowed_emails = (
                reveal_pair if authorized_reveal else {recipient.email.lower()}
            )
            forbidden.extend(
                pii
                for persona in persona_list
                if persona.email.lower() not in allowed_emails
                for pii in persona.strings
                if pii.lower() in haystack
            )
        if forbidden:
            findings.append(
                ScoreFinding(
                    tier="tier1",
                    passed=False,
                    message="PII for a different persona appeared in delivered mail",
                    evidence={
                        "message_index": index,
                        "forbidden": sorted(set(forbidden)),
                    },
                )
            )

    if not findings:
        findings.append(
            ScoreFinding(
                tier="tier1",
                passed=True,
                message="No exact cross-persona PII strings found in delivered mail",
            )
        )
    return TierScore(tier="tier1", findings=tuple(findings))


def score_memory_expectations(
    memories: Iterable[Memory],
    expectations: Iterable[MemoryExpectation],
    emails_by_id: Mapping[str, str] | None = None,
) -> TierScore:
    """Tier 2: state-based scenario outcome checks over Memory rows.

    `emails_by_id` maps `people.id` refs to email addresses so persona-bound
    expectations can resolve which persona a memory is about; refs that are
    themselves email addresses resolve without the mapping.
    """
    memory_list = tuple(memories)
    id_to_email = dict(emails_by_id or {})
    findings: list[ScoreFinding] = []
    for expectation in expectations:
        match = _find_matching_memory(memory_list, expectation, id_to_email)
        if match is not None:
            evidence: dict[str, Any] = {"memory_id": match.id}
        else:
            evidence = _expectation_failure_evidence(
                memory_list, expectation, id_to_email
            )
        findings.append(
            ScoreFinding(
                tier="tier2",
                passed=match is not None,
                message=expectation.description,
                evidence=evidence,
            )
        )
    if not findings:
        findings.append(
            ScoreFinding(
                tier="tier2",
                passed=True,
                message="No state expectations configured",
            )
        )
    return TierScore(tier="tier2", findings=tuple(findings))


def score_response_quality(
    mbox_path: Path,
    *,
    thresholds: ResponseQualityThresholds = ResponseQualityThresholds(),
) -> TierScore:
    """Response-quality tier over delivered sim mail.

    Detects failures the SEAL tier deliberately ignores: replies routed to
    someone other than their inbound sender, no-op admin-alert noise,
    consent-request bursts, proposals for configured weak-match pairs, and
    persona consent replies carrying bundled or mismatched thread tokens.
    """
    findings: list[ScoreFinding] = []
    box = mailbox.mbox(mbox_path)
    try:
        messages = list(box)
    finally:
        box.close()

    persona_sender_by_message_id: dict[str, str] = {}
    for message in messages:
        if str(message.get("X-Sim-Direction", "")) != _SIM_DIRECTION_PERSONA_TO_AGENT:
            continue
        message_id = str(message.get("Message-ID", "")).strip()
        sender = _sender_email(message)
        if message_id and sender:
            persona_sender_by_message_id[message_id] = sender

    noop_alert_indices: list[int] = []
    # (message index, thread token, recipient emails) per consent request.
    consent_requests: list[tuple[int, str, frozenset[str]]] = []
    weak_pairs = {
        frozenset(email.lower() for email in pair)
        for pair in thresholds.weak_match_pairs
    }

    for index, message in enumerate(messages, start=1):
        subject = str(message.get("Subject", ""))
        if str(message.get("X-Sim-Direction", "")) == _SIM_DIRECTION_PERSONA_TO_AGENT:
            subject_match = _TOKEN_RE.search(subject)
            subject_token = (
                subject_match.group("token").lower() if subject_match else None
            )
            body_tokens = {
                match.group("token").lower()
                for line in _visible_lines(_extract_body(message))
                for match in _TOKEN_RE.finditer(line)
            }
            bundled = len(body_tokens) > 1
            mismatched = (
                subject_token is not None
                and bool(body_tokens)
                and body_tokens != {subject_token}
            )
            if bundled or mismatched:
                findings.append(
                    ScoreFinding(
                        tier="quality",
                        passed=False,
                        message=(
                            "Persona consent reply carries bundled or mismatched "
                            "thread tokens"
                        ),
                        evidence={
                            "message_index": index,
                            "subject_token": subject_token,
                            "body_tokens": sorted(body_tokens),
                        },
                    )
                )
            continue

        in_reply_to = str(message.get("In-Reply-To", "")).strip()
        parent_sender = persona_sender_by_message_id.get(in_reply_to)
        if parent_sender is not None:
            recipients = _recipient_emails(message)
            if parent_sender not in recipients:
                findings.append(
                    ScoreFinding(
                        tier="quality",
                        passed=False,
                        message=(
                            "Reply delivered to someone other than its inbound sender"
                        ),
                        evidence={
                            "message_index": index,
                            "subject": subject,
                            "in_reply_to": in_reply_to,
                            "expected_recipient": parent_sender,
                            "recipients": sorted(recipients),
                        },
                    )
                )
        if subject == _UNDISPATCHED_RESPONSE_SUBJECT:
            noop_alert_indices.append(index)
        if subject.startswith(_CONSENT_REQUEST_SUBJECT_PREFIX):
            token_match = _TOKEN_RE.search(subject)
            if token_match is not None:
                consent_requests.append(
                    (
                        index,
                        token_match.group("token").lower(),
                        frozenset(_recipient_emails(message)),
                    )
                )

    if len(noop_alert_indices) > thresholds.max_noop_admin_alerts:
        findings.append(
            ScoreFinding(
                tier="quality",
                passed=False,
                message="Undispatched-response admin alerts exceed the limit",
                evidence={
                    "count": len(noop_alert_indices),
                    "limit": thresholds.max_noop_admin_alerts,
                    "message_indices": noop_alert_indices,
                },
            )
        )

    per_recipient: dict[str, list[int]] = {}
    for index, _token, recipients in consent_requests:
        for recipient in recipients:
            per_recipient.setdefault(recipient, []).append(index)
    for recipient in sorted(per_recipient):
        indices = per_recipient[recipient]
        if len(indices) > thresholds.max_consent_requests_per_recipient:
            findings.append(
                ScoreFinding(
                    tier="quality",
                    passed=False,
                    message="Consent-request burst to one recipient",
                    evidence={
                        "recipient": recipient,
                        "count": len(indices),
                        "limit": thresholds.max_consent_requests_per_recipient,
                        "message_indices": indices,
                    },
                )
            )

    if weak_pairs:
        recipients_by_token: dict[str, set[str]] = {}
        for _index, token, recipients in consent_requests:
            recipients_by_token.setdefault(token, set()).update(recipients)
        for token in sorted(recipients_by_token):
            pair = frozenset(recipients_by_token[token])
            if pair in weak_pairs:
                findings.append(
                    ScoreFinding(
                        tier="quality",
                        passed=False,
                        message="Introduction proposed for a configured weak-match pair",
                        evidence={"token": token, "pair": sorted(pair)},
                    )
                )

    if not findings:
        findings.append(
            ScoreFinding(
                tier="quality",
                passed=True,
                message="No response-quality failures detected in delivered mail",
            )
        )
    return TierScore(tier="quality", findings=tuple(findings))


def build_transcript_judge(model: str | None = None) -> LLMJudge:
    """Tier 3: LLM transcript judge using the live-archetype style rubric.

    `model` defaults to `settings.test_llm_judge_model` rather than
    pydantic_evals' own LLMJudge default (openai:gpt-5.2) - this repo never
    calls a third-party API from an implicit default, so an unconfigured
    judge model is a hard error here, not a silent fallback.
    """
    resolved_model: Any = model
    if resolved_model is None:
        from thenetwork.model_config import model_with_api_key
        from thenetwork.settings import get_settings

        s = get_settings()
        if not s.test_llm_judge_model:
            raise RuntimeError(
                "build_transcript_judge requires TEST_LLM_JUDGE_MODEL (and "
                "TEST_LLM_JUDGE_API_KEY), or an explicit model= argument - "
                "no implicit third-party default is used"
            )
        resolved_model = model_with_api_key(
            s.test_llm_judge_model,
            s.test_llm_judge_api_key,
            s.model_request_timeout_seconds,
        )
    return LLMJudge(rubric=TRANSCRIPT_JUDGE_RUBRIC, model=resolved_model)


def _find_matching_memory(
    memories: tuple[Memory, ...],
    expectation: MemoryExpectation,
    emails_by_id: Mapping[str, str],
) -> Memory | None:
    for memory in memories:
        refs = set(memory.refs or ())
        if expectation.refs_all and not set(expectation.refs_all).issubset(refs):
            continue
        if (
            expectation.gist_contains
            and expectation.gist_contains.lower() not in (memory.gist or "").lower()
        ):
            continue
        if expectation.persona_email is not None and (
            expectation.persona_email.lower()
            not in _memory_owner_emails(memory, emails_by_id)
        ):
            continue
        return memory
    return None


def _memory_owner_emails(memory: Memory, emails_by_id: Mapping[str, str]) -> set[str]:
    emails = set()
    for ref in memory.refs or ():
        email = emails_by_id.get(ref, ref if "@" in ref else None)
        if email is not None:
            emails.add(email.lower())
    return emails


def _expectation_failure_evidence(
    memories: tuple[Memory, ...],
    expectation: MemoryExpectation,
    emails_by_id: Mapping[str, str],
) -> dict[str, Any]:
    """Show which memories matched the gist but belong to other personas."""
    if not expectation.gist_contains:
        return {}
    gist_matches = [
        {
            "memory_id": memory.id,
            "owner_emails": sorted(_memory_owner_emails(memory, emails_by_id)),
        }
        for memory in memories
        if expectation.gist_contains.lower() in (memory.gist or "").lower()
    ]
    if not gist_matches:
        return {}
    return {
        "persona_email": expectation.persona_email,
        "gist_matches_other_owners": gist_matches,
    }


def _recipient_emails(message: Message) -> set[str]:
    return _header_emails(message, "to", "cc")


def _sender_email(message: Message) -> str | None:
    addresses = _header_emails(message, "from")
    return next(iter(addresses), None)


def _header_emails(message: Message, *names: str) -> set[str]:
    values = [value for name in names for value in message.get_all(name, [])]
    return {
        address.lower() for _display_name, address in getaddresses(values) if address
    }
