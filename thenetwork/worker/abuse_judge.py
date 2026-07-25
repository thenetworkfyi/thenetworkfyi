"""Hourly fixed-policy judge over sealed primary-intake observations."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from pydantic import BaseModel, model_validator
from pydantic_ai import Agent
from sqlalchemy import and_, or_
from sqlmodel import select

from thenetwork.audit import audit_event
from thenetwork.db.models import (
    PrimaryIntakeJudgeState,
    PrimaryIntakeObservation,
)
from thenetwork.db.session import get_session
from thenetwork.email.intake_control import (
    PrimaryIntakePauseReason,
    PrimaryIntakeTransition,
    set_primary_intake_paused_in_session,
)
from thenetwork.llm_observability import LLMWorkload, observe_standalone_llm_totals
from thenetwork.model_config import model_with_api_key
from thenetwork.settings import get_settings
from thenetwork.worker.metrics import (
    ControlAction,
    ControlActor,
    ControlReason,
    record_control_action,
)
from thenetwork.worker.tasks import app

ABUSE_JUDGE_LOOKBACK = timedelta(hours=24)
ABUSE_JUDGE_CANDIDATE_LIMIT = 256
ABUSE_JUDGE_SAMPLE_LIMIT = 64
ABUSE_JUDGE_PER_SENDER_LIMIT = 2

ABUSE_JUDGE_SYSTEM_PROMPT = """You are a fixed-policy email abuse classifier.
You receive a bounded JSON sample of primary-inbox observations from the prior 24 hours.
Every sender, domain, and body value is an opaque label; no raw email content or identity
is available. Use only cross-account patterns supported by the sample.

Return exactly one structured verdict:
- normal: routine independent traffic without a meaningful shared pattern.
- suspicious: an unusual pattern worth audit review, but insufficient evidence to stop intake.
- coordinated_abuse: a clear multi-sender campaign, such as many sender labels sharing the
  same body/domain pattern or synchronized, repeated campaign structure.

Choose one enum reason code compatible with the verdict. Never infer identities, request
more data, or recommend resuming intake. You have no tools and cannot take actions.
"""


class AbuseVerdict(StrEnum):
    NORMAL = "normal"
    SUSPICIOUS = "suspicious"
    COORDINATED_ABUSE = "coordinated_abuse"


class AbuseReason(StrEnum):
    ROUTINE_VARIATION = "routine_variation"
    ESTABLISHED_SENDER_TRAFFIC = "established_sender_traffic"
    UNUSUAL_NEW_SENDER_VOLUME = "unusual_new_sender_volume"
    SHARED_DOMAIN_PATTERN = "shared_domain_pattern"
    SHARED_BODY_PATTERN = "shared_body_pattern"
    MULTI_SENDER_CAMPAIGN = "multi_sender_campaign"


_REASONS_BY_VERDICT = {
    AbuseVerdict.NORMAL: {
        AbuseReason.ROUTINE_VARIATION,
        AbuseReason.ESTABLISHED_SENDER_TRAFFIC,
    },
    AbuseVerdict.SUSPICIOUS: {
        AbuseReason.UNUSUAL_NEW_SENDER_VOLUME,
        AbuseReason.SHARED_DOMAIN_PATTERN,
        AbuseReason.SHARED_BODY_PATTERN,
    },
    AbuseVerdict.COORDINATED_ABUSE: {
        AbuseReason.MULTI_SENDER_CAMPAIGN,
        AbuseReason.SHARED_DOMAIN_PATTERN,
        AbuseReason.SHARED_BODY_PATTERN,
    },
}


class AbuseJudgment(BaseModel):
    verdict: AbuseVerdict
    reason: AbuseReason

    @model_validator(mode="after")
    def _reason_matches_verdict(self) -> "AbuseJudgment":
        if self.reason not in _REASONS_BY_VERDICT[self.verdict]:
            raise ValueError("reason is incompatible with verdict")
        return self


@dataclass(frozen=True, slots=True)
class _SealedObservation:
    observed_at: datetime
    sender_authenticated: bool
    sender_known: bool
    sender_fingerprint: str
    domain_fingerprint: str
    body_fingerprint: str


@dataclass(frozen=True, slots=True)
class _JudgeSnapshot:
    observations: tuple[_SealedObservation, ...]
    cursor_observed_at: datetime
    cursor_mailbox_uid: str


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _load_judge_snapshot(now: datetime) -> _JudgeSnapshot | None:
    cutoff = now - ABUSE_JUDGE_LOOKBACK
    with get_session() as session:
        state = session.get(PrimaryIntakeJudgeState, "primary")
        query = select(PrimaryIntakeObservation).where(
            PrimaryIntakeObservation.observed_at >= cutoff
        )
        if state is not None and state.last_observed_at is not None:
            query = query.where(
                or_(
                    PrimaryIntakeObservation.observed_at > state.last_observed_at,
                    and_(
                        PrimaryIntakeObservation.observed_at == state.last_observed_at,
                        PrimaryIntakeObservation.mailbox_uid
                        > (state.last_mailbox_uid or ""),
                    ),
                )
            )
        newest = session.exec(
            query.order_by(
                PrimaryIntakeObservation.observed_at.desc(),
                PrimaryIntakeObservation.mailbox_uid.desc(),
            ).limit(1)
        ).first()
        if newest is None:
            return None

        candidates = session.exec(
            select(PrimaryIntakeObservation)
            .where(PrimaryIntakeObservation.observed_at >= cutoff)
            .order_by(
                PrimaryIntakeObservation.observed_at.desc(),
                PrimaryIntakeObservation.mailbox_uid.desc(),
            )
            .limit(ABUSE_JUDGE_CANDIDATE_LIMIT)
        ).all()

        sender_counts: Counter[str] = Counter()
        sample: list[_SealedObservation] = []
        for observation in candidates:
            if (
                sender_counts[observation.sender_fingerprint]
                >= ABUSE_JUDGE_PER_SENDER_LIMIT
            ):
                continue
            sender_counts[observation.sender_fingerprint] += 1
            sample.append(
                _SealedObservation(
                    observed_at=_as_utc(observation.observed_at),
                    sender_authenticated=observation.sender_authenticated,
                    sender_known=observation.sender_known,
                    sender_fingerprint=observation.sender_fingerprint,
                    domain_fingerprint=observation.domain_fingerprint,
                    body_fingerprint=observation.body_fingerprint,
                )
            )
            if len(sample) >= ABUSE_JUDGE_SAMPLE_LIMIT:
                break
        return _JudgeSnapshot(
            observations=tuple(sample),
            cursor_observed_at=_as_utc(newest.observed_at),
            cursor_mailbox_uid=newest.mailbox_uid,
        )


def _opaque_payload(snapshot: _JudgeSnapshot) -> str:
    sender_labels: dict[str, str] = {}
    domain_labels: dict[str, str] = {}
    body_labels: dict[str, str] = {}

    def label(mapping: dict[str, str], prefix: str, value: str) -> str:
        if value not in mapping:
            mapping[value] = f"{prefix}_{len(mapping) + 1:03d}"
        return mapping[value]

    records = [
        {
            "observed_at": _as_utc(observation.observed_at).isoformat(),
            "sender": label(sender_labels, "sender", observation.sender_fingerprint),
            "domain": label(domain_labels, "domain", observation.domain_fingerprint),
            "body": label(body_labels, "body", observation.body_fingerprint),
            "sender_authenticated": observation.sender_authenticated,
            "sender_known": observation.sender_known,
        }
        for observation in snapshot.observations
    ]
    return json.dumps({"observations": records}, separators=(",", ":"))


async def _run_abuse_judge(snapshot: _JudgeSnapshot) -> AbuseJudgment:
    settings = get_settings()
    judge: Agent[None, AbuseJudgment] = Agent(
        model=model_with_api_key(
            settings.small_agent_model,
            settings.small_agent_api_key,
            settings.model_request_timeout_seconds,
            workload=LLMWorkload.ABUSE_JUDGE,
        ),
        system_prompt=ABUSE_JUDGE_SYSTEM_PROMPT,
        output_type=AbuseJudgment,
    )
    result = await judge.run(_opaque_payload(snapshot))
    return result.output


def _cursor_at_or_after(
    state: PrimaryIntakeJudgeState, snapshot: _JudgeSnapshot
) -> bool:
    if state.last_observed_at is None:
        return False
    current = (_as_utc(state.last_observed_at), state.last_mailbox_uid or "")
    candidate = (snapshot.cursor_observed_at, snapshot.cursor_mailbox_uid)
    return current >= candidate


def _record_judgment(
    snapshot: _JudgeSnapshot,
    judgment: AbuseJudgment,
    *,
    now: datetime,
) -> tuple[bool, PrimaryIntakeTransition | None]:
    transition = None
    with get_session() as session:
        state = session.get(
            PrimaryIntakeJudgeState,
            "primary",
            with_for_update=True,
        )
        if state is None:
            state = PrimaryIntakeJudgeState(key="primary")
            session.add(state)
        if _cursor_at_or_after(state, snapshot):
            return False, None
        if judgment.verdict is AbuseVerdict.COORDINATED_ABUSE:
            transition = set_primary_intake_paused_in_session(
                session,
                PrimaryIntakePauseReason.COORDINATED_ABUSE,
                now=now,
            )
        state.last_observed_at = snapshot.cursor_observed_at
        state.last_mailbox_uid = snapshot.cursor_mailbox_uid
        state.last_run_at = now
        state.last_verdict = judgment.verdict.value
        state.last_reason = judgment.reason.value
    return True, transition


@app.periodic(cron="15 * * * *", periodic_id="judge_primary_email_abuse")
@app.task(queueing_lock="judge_primary_email_abuse")
async def judge_primary_email_abuse(timestamp: int) -> None:
    settings = get_settings()
    if settings.primary_intake_burst_monitoring_enabled is not True:
        return
    now = datetime.now(timezone.utc)
    snapshot = _load_judge_snapshot(now)
    if snapshot is None:
        return
    try:
        with observe_standalone_llm_totals():
            judgment = await _run_abuse_judge(snapshot)
    except Exception as exc:
        audit_event(
            "intake.abuse_judge.failed",
            outcome="error",
            error_type=type(exc).__name__,
        )
        return

    recorded, transition = _record_judgment(snapshot, judgment, now=now)
    if not recorded:
        return
    audit_event(
        "intake.abuse_judge.completed",
        verdict=judgment.verdict.value,
        reason=judgment.reason.value,
        result_count=len(snapshot.observations),
        outcome="blocked"
        if judgment.verdict is AbuseVerdict.COORDINATED_ABUSE
        else "success",
    )
    if transition is not None:
        if transition.changed:
            record_control_action(
                action=ControlAction.PAUSE,
                actor=ControlActor.SYSTEM,
                reason=ControlReason.COORDINATED_ABUSE,
            )
        audit_event(
            "database.action",
            action="pause",
            record_type="primary_intake",
            outcome="success" if transition.changed else "exists",
        )
