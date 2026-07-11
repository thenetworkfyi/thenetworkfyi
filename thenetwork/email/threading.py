"""Validation helpers for email threading headers."""

from __future__ import annotations


def clean_message_id(value: str | None) -> str | None:
    """Return a stripped Message-ID token, or None when unsafe for headers."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or "\r" in cleaned or "\n" in cleaned:
        return None
    if any(char.isspace() for char in cleaned):
        return None
    return cleaned


def clean_references(value: str | None) -> str | None:
    """Return a stripped single-space References chain, or None when unsafe."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or "\r" in cleaned or "\n" in cleaned:
        return None
    parts = cleaned.split(" ")
    if any(not part or any(char.isspace() for char in part) for part in parts):
        return None
    return " ".join(parts)
