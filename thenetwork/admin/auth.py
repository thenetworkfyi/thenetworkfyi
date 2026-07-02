"""Admin channel authentication.

An admin request requires all of:
  1. Sender email is in the ADMIN_EMAILS allowlist.
  2. Subject starts with "ADMIN:" (case-insensitive).
  3. Body contains "TS:", "NONCE:", "SIG:" lines where SIG is an
     HMAC-SHA256 over (subject, ts, nonce) keyed by ADMIN_TOKEN, ts is
     within ADMIN_REPLAY_WINDOW_SECONDS of now, and nonce hasn't been seen
     before (tracked in the admin_nonces table).

ADMIN_TOKEN is a signing key, never sent over the wire itself — unlike a
bearer token, a captured email doesn't hand over a durable, replayable
credential. Binding the subject into the signature also stops a captured
signature for one command being replayed against a different one.

Use sign_admin_request() (or `scripts/admin_sign.py`) to build the header a
real admin pastes into an email body.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from thenetwork.db.models import AdminNonce
from thenetwork.db.session import get_session
from thenetwork.settings import get_settings

_ADMIN_PREFIX = "admin:"
_TS_RE = re.compile(r"^TS:\s*(\d+)$")
_NONCE_RE = re.compile(r"^NONCE:\s*([A-Za-z0-9_-]{16,64})$")
_SIG_RE = re.compile(r"^SIG:\s*([0-9a-f]{64})$")


def _expected_signature(token: str, subject: str, ts: str, nonce: str) -> str:
    msg = f"{subject.strip()}\n{ts}\n{nonce}".encode()
    return hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


def sign_admin_request(token: str, subject: str) -> str:
    """Build the TS/NONCE/SIG lines to paste into an admin email body."""
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    sig = _expected_signature(token, subject, ts, nonce)
    return f"TS: {ts}\nNONCE: {nonce}\nSIG: {sig}"


def _extract_fields(body: str) -> tuple[str, str, str] | None:
    ts = nonce = sig = None
    for line in body.splitlines():
        stripped = line.strip()
        if m := _TS_RE.match(stripped):
            ts = m.group(1)
        elif m := _NONCE_RE.match(stripped):
            nonce = m.group(1)
        elif m := _SIG_RE.match(stripped):
            sig = m.group(1)
    if ts is None or nonce is None or sig is None:
        return None
    return ts, nonce, sig


def _consume_nonce(nonce: str, window_seconds: int) -> bool:
    """True if `nonce` is fresh. Persists it and prunes rows outside the
    replay window so the table stays small without a separate cleanup job."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    try:
        with get_session() as session:
            session.execute(delete(AdminNonce).where(AdminNonce.created_at < cutoff))
            if session.get(AdminNonce, nonce) is not None:
                return False
            session.add(AdminNonce(nonce=nonce))
        return True
    except IntegrityError:
        # Concurrent request claimed this nonce first — treat as replay.
        return False


def is_admin_request(sender_email: str, subject: str, body: str) -> bool:
    s = get_settings()
    if not s.admin_emails or not s.admin_token:
        return False
    if sender_email.lower() not in {e.lower() for e in s.admin_emails}:
        return False
    if not subject.strip().lower().startswith(_ADMIN_PREFIX):
        return False

    fields = _extract_fields(body)
    if fields is None:
        return False
    ts_str, nonce, sig = fields

    if abs(time.time() - int(ts_str)) > s.admin_replay_window_seconds:
        return False

    expected = _expected_signature(s.admin_token, subject, ts_str, nonce)
    if not hmac.compare_digest(sig, expected):
        return False

    return _consume_nonce(nonce, s.admin_replay_window_seconds)


def extract_command(subject: str) -> str:
    colon = subject.index(":")
    return subject[colon + 1:].strip()


def extract_body_text(body: str) -> str:
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("TS:", "NONCE:", "SIG:")):
            continue
        if stripped.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()
