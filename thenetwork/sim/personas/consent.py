"""Thread-faithful consent replies for simulated personas.

The tick loop presents at most one pending consent thread per persona turn.
Tokens copied from other threads are stripped, and a decision on the first
line carries exactly the answered thread's token on the second line.
"""

from __future__ import annotations

from email.message import EmailMessage
from re import Match

# The authoritative token and decision grammar lives with the production
# consent parser; reusing it keeps the sim from drifting out of sync with it.
from thenetwork.introductions import _ACTION_RE, _TOKEN_RE
from thenetwork.sim.run.mail import _extract_body

_ANY_TOKEN_RE = _TOKEN_RE


def intro_token(message: EmailMessage) -> str | None:
    """Return the consent token a message carries, if any."""
    match = _TOKEN_RE.search(str(message.get("Subject", "")))
    if match is not None:
        return match.group("token")
    for line in _visible_lines(_extract_body(message)):
        match = _TOKEN_RE.search(line)
        if match is not None:
            return match.group("token")
    return None


def thread_token_of(message: EmailMessage) -> tuple[str, str] | None:
    """Return the consent thread token carried by a message."""
    match = _ANY_TOKEN_RE.search(str(message.get("Subject", "")))
    if match is not None:
        return "intro", match.group("token")
    for line in _visible_lines(_extract_body(message)):
        match = _ANY_TOKEN_RE.search(line)
        if match is not None:
            return "intro", match.group("token")
    return None


def make_reply_thread_faithful(
    body: str, thread_token: str | None, kind: str = "intro"
) -> str:
    """Rewrite a persona-authored reply so any decision binds to one thread.

    Tokens other than `thread_token` are stripped wherever they appear. When
    the first line carries a decision, the reply is rebuilt with exactly one
    line containing the answered thread's token.
    """
    keep = (kind, thread_token) if thread_token is not None else None
    lines: list[str] = []
    for line in body.replace("\r", "").split("\n"):
        cleaned, removed = _strip_tokens(line, keep=keep)
        if removed and not cleaned.strip():
            continue
        lines.append(cleaned)
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return ""

    if thread_token is None or _ACTION_RE.search(lines[0]) is None:
        return "\n".join(lines)

    first, _ = _strip_tokens(lines[0], keep=None)
    rest: list[str] = []
    for line in lines[1:]:
        cleaned, removed = _strip_tokens(line, keep=None)
        if removed and not cleaned.strip():
            continue
        rest.append(cleaned)
    return "\n".join([first, f"[intro:{thread_token}]", *rest])


def _strip_tokens(line: str, *, keep: tuple[str, str] | None) -> tuple[str, bool]:
    removed = False

    def _replace(match: Match[str]) -> str:
        nonlocal removed
        if keep is not None and match.group("token").lower() == keep[1].lower():
            return match.group(0)
        removed = True
        return ""

    return _ANY_TOKEN_RE.sub(_replace, line).rstrip(), removed


def _visible_lines(body: str) -> list[str]:
    return [
        line.strip()
        for line in body.replace("\r", "").splitlines()
        if line.strip() and not line.lstrip().startswith(">")
    ]
