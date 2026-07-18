"""Tests for server-owned introduction relay addresses."""

import pytest

from thenetwork.email.relay import build_relay_address, parse_relay_address


TOKEN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DOMAIN = "relay.example.com"


def test_build_relay_address_uses_fixed_format():
    assert build_relay_address(TOKEN, DOMAIN) == f"hidden-{TOKEN}@{DOMAIN}"


def test_parse_relay_address_returns_canonical_token():
    address = build_relay_address(TOKEN, DOMAIN)

    assert parse_relay_address(address, DOMAIN) == TOKEN
    assert parse_relay_address(address.replace(DOMAIN, DOMAIN.upper()), DOMAIN) == TOKEN


@pytest.mark.parametrize(
    "address",
    [
        f"visible-{TOKEN}@{DOMAIN}",
        f"hidden-not-a-uuid@{DOMAIN}",
        f"hidden-{TOKEN}@other.example.com",
        f"The Network <hidden-{TOKEN}@{DOMAIN}>",
        f"hidden-{TOKEN.upper()}@{DOMAIN}",
        f" hidden-{TOKEN}@{DOMAIN}",
    ],
)
def test_parse_relay_address_rejects_nonconfigured_formats(address):
    assert parse_relay_address(address, DOMAIN) is None


@pytest.mark.parametrize("domain", ["", "relay@example.com", "bad domain", ".bad"])
def test_build_relay_address_rejects_invalid_domain(domain):
    with pytest.raises(ValueError):
        build_relay_address(TOKEN, domain)


def test_settings_exposes_relay_domain(monkeypatch):
    from thenetwork.settings import Settings

    monkeypatch.setenv("RELAY_DOMAIN", DOMAIN)
    settings = Settings(
        agent_model="test:model",
        small_agent_model="test:model",
        embed_model="test:embed",
    )

    assert settings.relay_domain == DOMAIN
