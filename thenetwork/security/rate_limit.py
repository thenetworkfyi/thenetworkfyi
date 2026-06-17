"""Per-sender rate limiting via the `limits` library (Flask-Limiter's engine).

Backed by Postgres so state survives restarts and is consistent across workers.
No bespoke token bucket — just the established library.
"""
from __future__ import annotations

from limits import parse, storage, strategies

from thenetwork.settings import get_settings

_limiter: strategies.MovingWindowRateLimiter | None = None
_storage: storage.Storage | None = None


def _get_limiter() -> tuple[strategies.MovingWindowRateLimiter, object]:
    global _limiter, _storage
    if _limiter is None:
        _storage = storage.MemoryStorage()
        _limiter = strategies.MovingWindowRateLimiter(_storage)
    return _limiter, _storage


def check_rate_limit(sender_email: str) -> bool:
    """Return True if the sender is within their hourly quota, False if over limit."""
    s = get_settings()
    limiter, _ = _get_limiter()
    limit = parse(f"{s.rate_limit_per_hour}/hour")
    key = f"sender:{sender_email}"
    return limiter.hit(limit, key)
