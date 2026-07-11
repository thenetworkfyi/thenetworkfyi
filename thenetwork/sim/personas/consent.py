"""Thread-faithful consent replies for simulated personas.

The tick loop presents at most one pending consent-request thread per persona
turn, and any consent decision a persona authors is bound to the thread it is
actually answering: tokens copied from other threads are stripped, and a
decision on the first line carries exactly the answered thread's token on the
second line. This keeps simulated replies honest inputs for the production
consent parser, which reads the decision from the first visible line and the
token from the subject or a visible line.
"""

from __future__ import annotations

from email.message import EmailMessage
from re import Match

# The authoritative token/decision grammar lives with the production consent
# parser; reusing it keeps the sim from drifting out of sync with it.
from thenetwork.introductions import _ACTION_RE, _TOKEN_RE
from thenetwork.sim.run.mail import _extract_body


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


def make_reply_thread_faithful(body: str, thread_token: str | None) -> str:
    """Rewrite a persona-authored reply so any decision binds to one thread.

    Tokens other than `thread_token` are stripped wherever they appear (a line
    reduced to nothing by the strip is dropped). When the first line carries a
    decision word, the reply is rebuilt as that line, then exactly one line
    with the thread's own token, then the rest with all other tokens removed.
    A reply with no decision on the first line keeps its own-thread token
    lines untouched, so a clarifying question still references its thread.
    """
    lines: list[str] = []
    for line in body.replace("\r", "").split("\n"):
        cleaned, removed = _strip_tokens(line, keep=thread_token)
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


def _strip_tokens(line: str, *, keep: str | None) -> tuple[str, bool]:
    removed = False

    def _replace(match: Match[str]) -> str:
        nonlocal removed
        if keep is not None and match.group("token").lower() == keep.lower():
            return match.group(0)
        removed = True
        return ""

    return _TOKEN_RE.sub(_replace, line).rstrip(), removed


def _visible_lines(body: str) -> list[str]:
    return [
        line.strip()
        for line in body.replace("\r", "").splitlines()
        if line.strip() and not line.lstrip().startswith(">")
    ]
