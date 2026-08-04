"""The SEAL's sanitization boundary: raw freeform text in, gist-safe text out.

One local span classifier does all of it: there is no pattern tier and no
per-write model call behind it.
"""

from __future__ import annotations

from functools import lru_cache

from sqlmodel import Session

from thenetwork.db.models import Memory

# Span labels redacted from anything that can cross a user boundary, mapped to
# the bracket-token style gists have always used.
#
# `private_date` is deliberately absent: "a Rust meetup Thursday" is
# perishability and recall signal, and `memories.created_at` already carries
# the recency the graph needs. Organizations and place names have no label in
# this taxonomy at all, which happens to match the long-standing decision to
# keep them for company/place search recall.
#
# `private_url` *is* redacted, unlike the pattern tier it replaces. A profile
# URL is a handle, and a handle names a real person outside this system. That
# costs us generic project URLs as recall text; leaking an identity costs more.
_ENTITY_LABELS = {
    "private_person": "[name]",
    "private_email": "[email]",
    "private_phone": "[phone]",
    "private_address": "[address]",
    "private_url": "[url]",
    "account_number": "[id]",
}


@lru_cache(maxsize=1)
def _get_privacy_filter():
    """Build and cache the local classifier every sanitized projection runs through.

    Weights are local and the model is ungated, so this needs no credential and
    makes no network call once cached. A missing model is a deployment error,
    not grounds for a silent downgrade: without it there is no sanitizer at all,
    and a cross-user gist would carry raw names.
    """
    from thenetwork.settings import get_settings

    try:
        from transformers import pipeline
    except ImportError as exc:
        raise RuntimeError("transformers is required for memory sanitization") from exc

    try:
        return pipeline(
            task="token-classification",
            model=get_settings().sanitize_model,
            aggregation_strategy="simple",
        )
    except Exception as exc:
        raise RuntimeError(
            "The sanitizer model could not load; check the model id and local cache"
        ) from exc


def assert_sanitizer_ready() -> None:
    """Fail fast at worker startup if the sanitizer cannot initialize."""
    _get_privacy_filter()


def classify_spans(text: str, labels) -> list[tuple[int, int, str]]:
    """Label `text` and return merged spans whose label is in `labels`.

    Shared with `thenetwork.security.log_redaction`, which keeps a different
    allow-list over the same taxonomy. Routing both through one function means
    one loaded copy of the weights rather than two.

    The model labels tokens, not words, so one value arrives in pieces: a name
    as ' mike_l' + 'ay', a phone as '415-555-267' + '1'. Splicing those
    separately would emit a token per fragment, and - worse - a fragment whose
    label fell outside the allow-list would leave part of the value behind.
    Adjacent spans sharing a label are therefore joined first.
    """
    ordered = sorted(
        (
            span
            for span in _get_privacy_filter()(text)
            if span["entity_group"] in labels
        ),
        key=lambda span: (span["start"], span["end"]),
    )
    merged: list[tuple[int, int, str]] = []
    for span in ordered:
        start, end, label = span["start"], span["end"], span["entity_group"]
        # The model folds the preceding space into a span; keeping it would
        # splice the separator away and run the token into the previous word.
        while start < end and text[start].isspace():
            start += 1
        if merged and merged[-1][2] == label and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), label)
        else:
            merged.append((start, end, label))
    return merged


def sanitize_text(text: str) -> str:
    """Return a PII-stripped projection of freeform content.

    This is the shared SEAL boundary for any freeform record that can cross a
    user boundary. Callers persist only the returned projection in searchable
    fields; the raw text stays confined to its owner-controlled record.
    """
    if not text.strip():
        return text
    spans = classify_spans(text, _ENTITY_LABELS)
    # Redact right-to-left so earlier spans' offsets stay valid.
    for start, end, label in sorted(spans, key=lambda span: span[0], reverse=True):
        text = text[:start] + _ENTITY_LABELS[label] + text[end:]
    return text


def sanitize_memory(memory: Memory, session: Session) -> str:
    """Produce and persist a gist for a person-referencing memory.

    Call for any memory with refs before it is eligible for cross-user search
    (SEAL requirement).
    """
    if not memory.refs:
        raise ValueError(
            f"Memory {memory.id} has no refs; only person-referencing memories require sanitization"
        )
    gist = sanitize_text(memory.text)
    memory.gist = gist
    session.add(memory)
    session.flush()
    return gist
