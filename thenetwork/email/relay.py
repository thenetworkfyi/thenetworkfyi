"""Server-owned addressing helpers for introduction email relays."""

from __future__ import annotations

import uuid
from typing import Callable

from sqlmodel import select

from thenetwork.db.models import IntroductionConsent, Person
from thenetwork.db.session import get_session

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


def is_relay_address_candidate(address: str | None, relay_domain: str) -> bool:
    """Identify catch-all traffic that must fail closed as a relay attempt."""

    if not address:
        return False
    try:
        domain = _normalise_domain(relay_domain)
    except ValueError:
        return False
    if address != address.strip() or address.count("@") != 1:
        return False
    local_part, candidate_domain = address.rsplit("@", 1)
    return local_part.lower().startswith(_PREFIX) and (
        candidate_domain.lower().rstrip(".") == domain
    )


def resolve_relay_destination(
    *,
    recipient_address: str,
    sender_email: str,
    sender_authenticated: bool,
    relay_domain: str,
    session_factory: Callable | None = None,
) -> str | None:
    """Resolve an introduced pair to only its opposite participant's address."""

    if not sender_authenticated:
        return None
    token = parse_relay_address(recipient_address, relay_domain)
    if token is None:
        return None

    session_factory = session_factory or get_session
    with session_factory() as session:
        consent = session.exec(
            select(IntroductionConsent).where(
                IntroductionConsent.reply_token == token,
                IntroductionConsent.status == "introduced",
            )
        ).first()
        if consent is None or consent.status != "introduced":
            return None

        person_a = session.get(Person, consent.person_a_id)
        person_b = session.get(Person, consent.person_b_id)
        if person_a is None or person_b is None:
            return None
        if sender_email == person_a.email:
            return person_b.email
        if sender_email == person_b.email:
            return person_a.email
        return None
