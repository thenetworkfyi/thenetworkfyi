"""Keyed, non-reversible fingerprints for primary-intake observations."""

from __future__ import annotations

import base64
import hashlib
import hmac

from thenetwork.security.sender_identifier import (
    SenderIdentifierSecretMissing,
    normalize_sender_identifier_identity,
)

_DIGEST_BYTES = 16


def intake_fingerprints(
    sender_email: str, body: str, *, secret: str | bytes
) -> tuple[str, str, str]:
    if isinstance(secret, str):
        key = secret.encode("utf-8")
    else:
        key = secret
    if not key:
        raise SenderIdentifierSecretMissing("sender_identifier_secret is required")

    sender = normalize_sender_identifier_identity(sender_email)
    _, separator, domain = sender.rpartition("@")
    if not separator or not domain:
        domain = "unknown"
    return (
        _fingerprint(key, b"sender", sender.encode("utf-8")),
        _fingerprint(key, b"domain", domain.encode("utf-8")),
        _fingerprint(key, b"body", body.encode("utf-8")),
    )


def _fingerprint(key: bytes, kind: bytes, value: bytes) -> str:
    digest = hmac.digest(
        key,
        b"thenetwork.primary_intake.v1\0" + kind + b"\0" + value,
        hashlib.sha256,
    )
    encoded = base64.urlsafe_b64encode(digest[:_DIGEST_BYTES]).decode().rstrip("=")
    return f"intake_{kind.decode()}_v1_{encoded}"
