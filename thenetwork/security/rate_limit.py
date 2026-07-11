"""Durable inbound email rate limiting via the `limits` library."""
from __future__ import annotations

from email.utils import parseaddr
import unicodedata

from limits import parse, strategies
from limits.storage import Storage
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from thenetwork.db.session import get_engine
from thenetwork.settings import get_settings

_limiter: strategies.FixedWindowRateLimiter | None = None
_storage: Storage | None = None


class PostgresFixedWindowStorage(Storage):
    """`limits` fixed-window storage backed by the app Postgres database."""

    STORAGE_SCHEME = ["thenetwork-postgres"]

    def __init__(self) -> None:
        super().__init__()
        self._engine = get_engine()

    @property
    def base_exceptions(self) -> type[Exception] | tuple[type[Exception], ...]:
        return SQLAlchemyError

    def incr(self, key: str, expiry: int, amount: int = 1) -> int:
        with self._engine.begin() as conn:
            return int(
                conn.execute(
                    text(
                        """
                        INSERT INTO rate_limits (key, count, expires_at)
                        VALUES (
                            :key,
                            :amount,
                            now() + (:expiry * INTERVAL '1 second')
                        )
                        ON CONFLICT (key) DO UPDATE
                        SET
                            count = CASE
                                WHEN rate_limits.expires_at <= now()
                                    THEN :amount
                                ELSE rate_limits.count + :amount
                            END,
                            expires_at = CASE
                                WHEN rate_limits.expires_at <= now()
                                    THEN now() + (:expiry * INTERVAL '1 second')
                                ELSE rate_limits.expires_at
                            END
                        RETURNING count
                        """
                    ),
                    {"key": key, "amount": amount, "expiry": expiry},
                ).scalar_one()
            )

    def get(self, key: str) -> int:
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT count
                    FROM rate_limits
                    WHERE key = :key AND expires_at > now()
                    """
                ),
                {"key": key},
            ).first()
        return int(row[0]) if row else 0

    def get_expiry(self, key: str) -> float:
        with self._engine.begin() as conn:
            expires_at = conn.execute(
                text(
                    """
                    SELECT EXTRACT(EPOCH FROM expires_at)
                    FROM rate_limits
                    WHERE key = :key
                    """
                ),
                {"key": key},
            ).scalar_one_or_none()
        return float(expires_at or 0)

    def check(self) -> bool:
        with self._engine.begin() as conn:
            conn.execute(text("SELECT 1"))
        return True

    def reset(self) -> int | None:
        with self._engine.begin() as conn:
            result = conn.execute(text("DELETE FROM rate_limits"))
            return result.rowcount

    def clear(self, key: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM rate_limits WHERE key = :key"), {"key": key})


def _get_limiter() -> tuple[strategies.FixedWindowRateLimiter, Storage]:
    global _limiter, _storage
    if _limiter is None:
        _storage = PostgresFixedWindowStorage()
        _limiter = strategies.FixedWindowRateLimiter(_storage)
    return _limiter, _storage


# RFC 5233 sub-addressing ("+tag") is honored by essentially every major
# provider (Gmail, Outlook, Fastmail, ProtonMail, iCloud custom domains, ...),
# so stripping it is safe across the board. Dot-insensitivity and the "-tag"
# separator are provider-specific quirks, so they're scoped to the domains
# that actually implement them rather than applied universally.
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
_HYPHEN_SUBADDRESS_DOMAINS = frozenset({
    "yahoo.com", "yahoo.co.uk", "yahoo.ca", "yahoo.com.au", "yahoo.de", "yahoo.fr",
    "ymail.com", "rocketmail.com", "aol.com",
})


def normalize_rate_limit_identity(sender_email: str) -> str:
    """Return the canonical, non-empty email identity used in quota keys.

    Collapses common alias conventions (RFC 5233 "+tag" sub-addressing;
    Gmail's dot-insensitivity and gmail.com/googlemail.com equivalence;
    Yahoo/AOL's "-tag" sub-addressing) so a single mailbox can't mint
    unlimited distinct rate-limit identities. This governs quota bucketing
    only, never the address mail is actually sent to.
    """
    raw = unicodedata.normalize("NFKC", sender_email).strip()
    _, parsed = parseaddr(raw)
    normalized = (parsed or raw).strip().casefold()
    if not normalized or "@" not in normalized:
        return normalized or "unknown"

    local, _, domain = normalized.rpartition("@")
    local = local.split("+", 1)[0]

    if domain in _HYPHEN_SUBADDRESS_DOMAINS:
        local = local.split("-", 1)[0]

    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"

    if not local or not domain:
        return "unknown"
    return f"{local}@{domain}"


def _sender_key(sender_email: str, *, sender_authenticated: bool) -> str:
    identity = normalize_rate_limit_identity(sender_email)
    prefix = "authenticated-sender" if sender_authenticated else "unauthenticated-sender"
    return f"{prefix}:{identity}"


def check_rate_limit(
    sender_email: str,
    *,
    sender_authenticated: bool = True,
    skip_sender_limit: bool = False,
) -> bool:
    """Return True when applicable sender and global hourly quotas allow processing.

    Synthetic proactive jobs run on behalf of an already-selected person rather
    than an inbound sender. They skip only the per-sender bucket, while still
    consuming the global budget that bounds total agent work.
    """
    s = get_settings()
    sender_quota = (
        s.rate_limit_per_hour
        if sender_authenticated
        else s.unauthenticated_rate_limit_per_hour
    )
    sender_limit = parse(f"{sender_quota}/hour")
    global_limit = parse(f"{s.global_email_rate_limit_per_hour}/hour")
    sender_key = _sender_key(sender_email, sender_authenticated=sender_authenticated)
    global_key = "global:emails-processed"

    try:
        limiter, limit_storage = _get_limiter()
        if not limit_storage.check():
            return False
        if not skip_sender_limit and not limiter.test(sender_limit, sender_key):
            return False
        if not limiter.test(global_limit, global_key):
            return False
        if not skip_sender_limit and not limiter.hit(sender_limit, sender_key):
            return False
        return limiter.hit(global_limit, global_key)
    except Exception:
        return False
