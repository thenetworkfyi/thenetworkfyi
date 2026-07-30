"""Shared response conventions for model-backed simulation personas."""

PASS_SENTINEL = "PASS"


def _is_pass_sentinel(text: str) -> bool:
    """Recognize malformed sentinel replies without suppressing normal email text."""
    if not text:
        return True
    first_line = text.split("\n", maxsplit=1)[0].strip()
    return first_line.upper().startswith(PASS_SENTINEL)
