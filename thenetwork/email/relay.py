"""Server-owned addressing helpers for introduction email relays."""

from __future__ import annotations

import uuid

_PREFIX = "hidden-"


def _normalise_domain(relay_domain: str) -> str:
    domain = relay_domain.strip().lower().rstrip(".")
    if (
        not domain
        or "@" in domain
        or any(character.isspace() for character in domain)
        or domain.startswith(".")
        or ".." in domain
    ):
        raise ValueError("relay_domain must be a bare email domain")
    return domain


def build_relay_address(reply_token: str, relay_domain: str) -> str:
    """Build the fixed proxy address for an introduction consent pair."""

    token = str(uuid.UUID(reply_token))
    return f"{_PREFIX}{token}@{_normalise_domain(relay_domain)}"


def parse_relay_address(address: str, relay_domain: str) -> str | None:
    """Return a canonical pair token only for this deployment's proxy format."""

    try:
        domain = _normalise_domain(relay_domain)
    except ValueError:
        return None

    if address != address.strip() or address.count("@") != 1:
        return None
    local_part, candidate_domain = address.rsplit("@", 1)
    if candidate_domain.lower().rstrip(".") != domain or not local_part.startswith(
        _PREFIX
    ):
        return None

    token = local_part.removeprefix(_PREFIX)
    try:
        canonical = str(uuid.UUID(token))
    except (ValueError, AttributeError):
        return None
    return canonical if token == canonical else None
