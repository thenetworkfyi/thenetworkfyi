"""Durable daily LLM token budget, built on the shared rate-limit storage.

Separate from `rate_limit.py`'s per-sender/global email-count quotas: this
bucket meters model token spend and is consumed by observability code
(`thenetwork/llm_observability.py`) that has no inbound-email context to key
off. It reuses `PostgresFixedWindowStorage` and the existing `rate_limits`
table - no new table, no migration.
"""

from __future__ import annotations

from limits import parse, strategies

from thenetwork.security.rate_limit import PostgresFixedWindowStorage

_limiter: strategies.FixedWindowRateLimiter | None = None
_storage: PostgresFixedWindowStorage | None = None

_BUDGET_KEY = "daily-agent-token-budget"


def _get_limiter() -> tuple[
    strategies.FixedWindowRateLimiter, PostgresFixedWindowStorage
]:
    global _limiter, _storage
    if _limiter is None:
        _storage = PostgresFixedWindowStorage()
        _limiter = strategies.FixedWindowRateLimiter(_storage)
    return _limiter, _storage


def check_daily_token_budget(cap: int) -> bool:
    """Return whether the daily token budget still has headroom.

    Does not consume anything. `cap` must be read fresh from settings by the
    caller on every call - never captured in a module-level constant, default
    argument, or cache, since it can change at runtime (tests, sim overrides).
    A cap <= 0 disables the budget (always reports headroom). Fails closed
    (no headroom) if the durable storage is unavailable, matching the
    fail-closed convention of the other rate limiters in this codebase.
    """
    if cap <= 0:
        return True
    limiter, _ = _get_limiter()
    try:
        return limiter.test(parse(f"{cap}/day"), _BUDGET_KEY)
    except Exception:
        return False


def consume_daily_token_budget(tokens: int, cap: int) -> bool:
    """Atomically debit `tokens` from the daily budget; return whether still within cap.

    Always debits the actual tokens spent, even past the cap - the return
    value is informational for a future enforcement path, not a rejection.
    `cap` must be read fresh by the caller (see `check_daily_token_budget`).
    A cap <= 0 or non-positive `tokens` is a no-op that reports headroom.
    Never raises: this is telemetry-adjacent bookkeeping and must not alter
    email processing or model retry behavior if the durable store is briefly
    unavailable.
    """
    if cap <= 0 or tokens <= 0:
        return True
    limiter, _ = _get_limiter()
    try:
        return limiter.hit(parse(f"{cap}/day"), _BUDGET_KEY, cost=tokens)
    except Exception:
        return False
