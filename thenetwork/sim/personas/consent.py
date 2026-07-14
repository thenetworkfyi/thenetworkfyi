"""Thread-faithful consent and digest replies for simulated personas.

The tick loop presents at most one pending thread (a `[intro:...]` consent
request or a `[digest:...]` introduction digest) per persona turn, and any
decision a persona authors is bound to the thread it is actually answering:
tokens copied from other threads are stripped, and a decision on the first
line carries exactly the answered thread's token on the second line. This
keeps simulated replies honest inputs for the production parsers, which read
the decision from the first visible line and the token from the subject or a
visible line.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from re import Match

# The authoritative token/decision grammar lives with the production consent
# and digest parsers; reusing it keeps the sim from drifting out of sync with
# them.
from thenetwork.introductions import (
    _ACTION_RE,
    _DIGEST_SELECTION_RE,
    _DIGEST_TOKEN_RE,
    _TOKEN_RE,
)
from thenetwork.sim.run.mail import _extract_body

_ANY_TOKEN_RE = re.compile(
    r"\[(?P<kind>intro|digest):(?P<token>[0-9a-f]{8}-[0-9a-f-]{27,})\]",
    re.IGNORECASE,
)

_DECISION_PATTERNS = {"intro": _ACTION_RE, "digest": _DIGEST_SELECTION_RE}


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


def digest_token(message: EmailMessage) -> str | None:
    """Return the digest token a message carries, if any."""
    match = _DIGEST_TOKEN_RE.search(str(message.get("Subject", "")))
    if match is not None:
        return match.group("token")
    for line in _visible_lines(_extract_body(message)):
        match = _DIGEST_TOKEN_RE.search(line)
        if match is not None:
            return match.group("token")
    return None


def thread_token_of(message: EmailMessage) -> tuple[str, str] | None:
    """Return `(kind, token)` for whichever consent/digest thread this carries.

    `kind` is `"intro"` or `"digest"`. Returns `None` for a message with
    neither a `[intro:...]` nor a `[digest:...]` token.
    """
    match = _ANY_TOKEN_RE.search(str(message.get("Subject", "")))
    if match is not None:
        return match.group("kind").lower(), match.group("token")
    for line in _visible_lines(_extract_body(message)):
        match = _ANY_TOKEN_RE.search(line)
        if match is not None:
            return match.group("kind").lower(), match.group("token")
    return None


def make_reply_thread_faithful(
    body: str, thread_token: str | None, kind: str = "intro"
) -> str:
    """Rewrite a persona-authored reply so any decision binds to one thread.

    `kind` is `"intro"` (decision word yes/no/revoke) or `"digest"` (a letter
    selection or NONE); it selects both the decision grammar and the bracket
    text (`[intro:...]` vs `[digest:...]`) written back. Tokens other than
    `thread_token` are stripped wherever they appear (a line reduced to
    nothing by the strip is dropped). When the first line carries a decision,
    the reply is rebuilt as that line, then exactly one line with the
    thread's own token, then the rest with all other tokens removed. A reply
    with no decision on the first line keeps its own-thread token lines
    untouched, so a clarifying question still references its thread.
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

    decision_re = _DECISION_PATTERNS[kind]
    if thread_token is None or decision_re.search(lines[0]) is None:
        return "\n".join(lines)

    first, _ = _strip_tokens(lines[0], keep=None)
    rest: list[str] = []
    for line in lines[1:]:
        cleaned, removed = _strip_tokens(line, keep=None)
        if removed and not cleaned.strip():
            continue
        rest.append(cleaned)
    return "\n".join([first, f"[{kind}:{thread_token}]", *rest])


def _strip_tokens(line: str, *, keep: tuple[str, str] | None) -> tuple[str, bool]:
    removed = False

    def _replace(match: Match[str]) -> str:
        nonlocal removed
        if (
            keep is not None
            and match.group("kind").lower() == keep[0]
            and match.group("token").lower() == keep[1].lower()
        ):
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
