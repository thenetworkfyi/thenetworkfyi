"""Non-reversible sender identifiers for audit correlation."""
from __future__ import annotations

import base64
from email.utils import parseaddr
import hashlib
import hmac
import unicodedata

from thenetwork.settings import get_settings

SENDER_IDENTIFIER_PREFIX = "snd_v1"
_HMAC_CONTEXT = b"thenetwork.sender_identifier.v1\0"
_DIGEST_BYTES = 16


class SenderIdentifierSecretMissing(ValueError):
    """Raised when sender identifier derivation has no server-side secret."""


def normalize_sender_identifier_identity(sender_email: str) -> str:
    """Return the canonical email identity used for audit pseudonyms.

    This intentionally applies only syntax-level normalization: display-name
    parsing, Unicode normalization, whitespace trimming, and case folding.
    Provider-specific alias rules are for rate limiting, not audit identity.
    """
    raw = unicodedata.normalize("NFKC", sender_email).strip()
    _, parsed = parseaddr(raw)
    normalized = (parsed or raw).strip().casefold()
    return normalized or "unknown"


def sender_identifier(sender_email: str, *, secret: str | bytes | None = None) -> str:
    """Return a stable, keyed, truncated identifier for a sender address.

    The raw email is never embedded in the returned value. The same normalized
    sender and secret produce the same token, while changing either changes the
    token. A missing secret is a configuration error because an unkeyed digest
    of an email address is reversible by dictionary lookup.
    """
    if secret is None:
        secret = get_settings().sender_identifier_secret
    if isinstance(secret, str):
        key = secret.encode("utf-8")
    else:
        key = secret
    if not key:
        raise SenderIdentifierSecretMissing("sender_identifier_secret is required")

    identity = normalize_sender_identifier_identity(sender_email).encode("utf-8")
    digest = hmac.digest(key, _HMAC_CONTEXT + identity, hashlib.sha256)
    token = base64.urlsafe_b64encode(digest[:_DIGEST_BYTES]).decode("ascii").rstrip("=")
    return f"{SENDER_IDENTIFIER_PREFIX}_{token}"


def optional_sender_identifier(sender_email: str) -> str | None:
    """Return the configured sender identifier, or None when no secret is set.

    The audit layer must never silently downgrade to an unkeyed hash. Without
    the server-side secret, there is no safe sender pseudonym to emit.
    """
    try:
        return sender_identifier(sender_email)
    except SenderIdentifierSecretMissing:
        return None
