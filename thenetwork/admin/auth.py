"""Admin channel authentication.

An admin request requires all three:
  1. Sender email is in the ADMIN_EMAILS allowlist.
  2. Subject starts with "ADMIN:" (case-insensitive).
  3. Body contains a line matching "TOKEN: <admin_token>".

The shared-secret token is the explicit per-request credential that prevents
allowlisted-address spoofing.
"""
from __future__ import annotations
import secrets
from thenetwork.settings import get_settings

_ADMIN_PREFIX = "admin:"


def _token_line(body: str, token: str) -> bool:
    needle = f"TOKEN: {token}"
    for line in body.splitlines():
        if secrets.compare_digest(line.strip(), needle):
            return True
    return False


def is_admin_request(sender_email: str, subject: str, body: str) -> bool:
    s = get_settings()
    if not s.admin_emails or not s.admin_token:
        return False
    if sender_email.lower() not in {e.lower() for e in s.admin_emails}:
        return False
    if not subject.strip().lower().startswith(_ADMIN_PREFIX):
        return False
    return _token_line(body, s.admin_token)


def extract_command(subject: str) -> str:
    colon = subject.index(":")
    return subject[colon + 1:].strip()


def extract_body_text(body: str) -> str:
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("TOKEN:"):
            continue
        if stripped.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()
