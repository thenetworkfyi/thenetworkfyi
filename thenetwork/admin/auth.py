"""Admin channel authentication - PGP/MIME (RFC 3156) signature verification.

An admin request requires all of:
  1. Sender email is in the ADMIN_EMAILS allowlist.
  2. Subject starts with "ADMIN:" (case-insensitive) - a cheap pre-filter so
     we don't run gpg on every inbound email. The subject carries no
     authority beyond that filter: RFC 3156 signs only the MIME body, never
     message headers, so a header is not protected by the signature.
  3. The message is `multipart/signed; protocol="application/pgp-signature"`
     and the detached signature verifies against ADMIN_GPG_PUBLIC_KEY, using
     the byte-exact original signed part (re-serializing the parsed
     email.message.Message does not round-trip byte-exact - CRLF normalizes
     to LF - so the signed content is sliced directly out of the raw bytes).
  4. The verified cleartext body contains a "COMMAND:" line, and the
     signature itself is fresh and unused: the OpenPGP signature packet
     carries its own creation timestamp (must be within
     ADMIN_REPLAY_WINDOW_SECONDS of now) and its signature bytes hash to a
     value not seen before (tracked in the admin_nonces table). Both values
     come from inside the verified signature, not operator-authored text, so
     neither can be forged without invalidating the signature - no hand-typed
     TS/NONCE lines needed.

The command comes from the signed body's COMMAND: line, never the Subject
header. Since PGP/MIME never signs headers, trusting extract_command(subject)
would let an in-transit rewrite of Subject swap which command runs without
invalidating the signature. Likewise, replay protection can't key off any
unsigned header (e.g. Message-ID): an attacker can rewrite unsigned headers
on a captured signed email while the signed body+signature stay valid, so the
dedup key has to come from inside the signature itself.

Sign with any standard PGP/MIME-capable mail client (Thunderbird, Apple Mail
+ GPGSuite, etc.) - compose the body as:

    COMMAND: <verb> <args>

    <optional free text for remember/forget payloads>

and use the client's normal "digitally sign" action. No token to generate,
nothing to run by hand.
"""

from __future__ import annotations

import email
import hashlib
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone

import gnupg
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from thenetwork.db.models import AdminNonce
from thenetwork.db.session import get_session
from thenetwork.settings import get_settings

_ADMIN_PREFIX = "admin:"
_COMMAND_RE = re.compile(r"^COMMAND:\s*(.+?)\s*$", re.MULTILINE)

_gpg_instance: gnupg.GPG | None = None
_gpg_key_material: str | None = None


def _get_gpg(public_key: str) -> gnupg.GPG:
    """Return a GPG instance whose keyring holds only the configured admin key.

    Uses a dedicated, process-local gnupghome (never the system default) so
    verification can only ever succeed against the one key operators
    configured via ADMIN_GPG_PUBLIC_KEY, and re-imports if that setting
    changes (e.g. between tests).
    """
    global _gpg_instance, _gpg_key_material
    if _gpg_instance is None or _gpg_key_material != public_key:
        home = tempfile.mkdtemp(prefix="thenetwork-admin-gpg-")
        _gpg_instance = gnupg.GPG(gnupghome=home)
        _gpg_instance.import_keys(public_key)
        _gpg_key_material = public_key
    return _gpg_instance


def _extract_multipart_signed(raw_message: bytes) -> tuple[bytes, bytes] | None:
    """Split a raw multipart/signed MIME message into (signed content bytes,
    detached signature bytes), preserving the signed part's exact original
    bytes.

    RFC 3156 signatures are computed over the byte-exact original MIME part;
    re-encoding through email.message.Message.as_bytes() does not round-trip
    byte-exact (CRLF normalizes to LF), so the signed content is sliced
    directly out of the raw message instead of reconstructed from the parsed
    object.
    """
    msg = email.message_from_bytes(raw_message)
    if msg.get_content_type() != "multipart/signed":
        return None
    protocol = msg.get_param("protocol")
    if not isinstance(protocol, str) or protocol.lower() != "application/pgp-signature":
        return None
    boundary = msg.get_boundary()
    if not boundary:
        return None
    parts = msg.get_payload()
    if not isinstance(parts, list) or len(parts) != 2:
        return None
    sig_bytes = parts[1].get_payload(decode=True)
    if not sig_bytes:
        return None

    boundary_marker = ("--" + boundary).encode()
    first = raw_message.find(boundary_marker)
    if first == -1:
        return None
    content_start = raw_message.find(b"\n", first) + 1
    second = raw_message.find(boundary_marker, content_start)
    if second == -1:
        return None
    content_end = second
    if raw_message[:content_end].endswith(b"\r\n"):
        content_end -= 2
    elif raw_message[:content_end].endswith(b"\n"):
        content_end -= 1
    return raw_message[content_start:content_end], sig_bytes


def _verify_pgp_mime(
    raw_message: bytes, public_key: str
) -> tuple[bytes, str, int] | None:
    """Return (verified signed content, signature digest, signature
    timestamp), or None if invalid.

    The digest (SHA-256 of the detached signature bytes) and timestamp (the
    OpenPGP signature packet's own creation time) both come from inside the
    thing gpg just verified - neither can be forged without invalidating the
    signature, so they stand in for an operator-authored nonce/timestamp.
    """
    extracted = _extract_multipart_signed(raw_message)
    if extracted is None:
        return None
    signed_content, sig_bytes = extracted

    gpg = _get_gpg(public_key)
    with tempfile.NamedTemporaryFile(suffix=".sig") as sig_file:
        sig_file.write(sig_bytes)
        sig_file.flush()
        verified = gpg.verify_data(sig_file.name, signed_content)
    if not verified.valid:
        return None
    timestamp = verified.timestamp or verified.sig_timestamp
    if timestamp is None:
        return None
    sig_hash = hashlib.sha256(sig_bytes).hexdigest()
    return signed_content, sig_hash, int(timestamp)


def _consume_signature(sig_hash: str, window_seconds: int) -> bool:
    """True if `sig_hash` (digest of a verified signature) hasn't been seen
    before. Persists it and prunes rows outside the replay window so the
    table stays small without a separate cleanup job."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    try:
        with get_session() as session:
            session.exec(delete(AdminNonce).where(AdminNonce.created_at < cutoff))
            if session.get(AdminNonce, sig_hash) is not None:
                return False
            session.add(AdminNonce(nonce=sig_hash))
        return True
    except IntegrityError:
        # Concurrent request claimed this signature first - treat as replay.
        return False


def verify_admin_request(
    sender_email: str, subject: str, raw_message: bytes | None
) -> str | None:
    """Return the verified cleartext body if this is a valid signed admin
    request, else None. Consumes the signature's digest on success (single
    use)."""
    s = get_settings()
    if not s.admin_emails or not s.admin_gpg_public_key:
        return None
    if sender_email.lower() not in {e.lower() for e in s.admin_emails}:
        return None
    if not subject.strip().lower().startswith(_ADMIN_PREFIX):
        return None
    if raw_message is None:
        return None

    verified = _verify_pgp_mime(raw_message, s.admin_gpg_public_key)
    if verified is None:
        return None
    signed_content, sig_hash, sig_timestamp = verified

    if abs(time.time() - sig_timestamp) > s.admin_replay_window_seconds:
        return None

    content_msg = email.message_from_bytes(signed_content)
    payload = content_msg.get_payload(decode=True)
    if not payload:
        return None
    charset = content_msg.get_content_charset() or "utf-8"
    try:
        cleartext = payload.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return None

    if not _COMMAND_RE.search(cleartext):
        return None

    if not _consume_signature(sig_hash, s.admin_replay_window_seconds):
        return None

    return cleartext


def extract_command(cleartext: str) -> str:
    m = _COMMAND_RE.search(cleartext)
    return m.group(1).strip() if m else ""


def extract_body_text(cleartext: str) -> str:
    lines = []
    for line in cleartext.splitlines():
        stripped = line.strip()
        if _COMMAND_RE.match(stripped):
            continue
        if stripped.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()
