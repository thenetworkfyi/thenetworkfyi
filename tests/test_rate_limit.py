from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text


class FakeLimiter:
    def __init__(self, *, test_results: dict[str, bool] | None = None) -> None:
        self.test_results = test_results or {}
        self.tested: list[tuple[str, str]] = []
        self.hit_keys: list[str] = []

    def test(self, limit, key: str) -> bool:
        self.tested.append((str(limit), key))
        return self.test_results.get(key, True)

    def hit(self, limit, key: str) -> bool:
        self.hit_keys.append(key)
        return True


class HealthyStorage:
    def check(self) -> bool:
        return True


class UnavailableLimiter:
    def test(self, limit, key: str) -> bool:
        raise RuntimeError("rate-limit storage unavailable")

    def hit(self, limit, key: str) -> bool:
        raise RuntimeError("rate-limit storage unavailable")


def _settings(**overrides):
    defaults = {
        "rate_limit_per_hour": 20,
        "unauthenticated_rate_limit_per_hour": 6,
        "global_email_rate_limit_per_hour": 200,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_rate_limit_normalizes_sender_keys():
    from thenetwork.security.rate_limit import check_rate_limit

    limiter = FakeLimiter()

    with (
        patch("thenetwork.security.rate_limit.get_settings", return_value=_settings()),
        patch(
            "thenetwork.security.rate_limit._get_limiter",
            return_value=(limiter, HealthyStorage()),
        ),
    ):
        assert check_rate_limit(" Alice@Example.COM ", sender_authenticated=True)

    assert "authenticated-sender:alice@example.com" in limiter.hit_keys


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("alice+test@example.com", "alice@example.com"),
        ("alice+test1@gmail.com", "alice@gmail.com"),
        ("a.l.ice+tag@gmail.com", "alice@gmail.com"),
        ("alice@googlemail.com", "alice@gmail.com"),
        ("Alice.Test@GMAIL.com", "alicetest@gmail.com"),
        ("alice-tag@yahoo.com", "alice@yahoo.com"),
        ("alice-tag@aol.com", "alice@aol.com"),
        # dots and hyphens are literal outside the domains that special-case them
        ("alice.test@example.com", "alice.test@example.com"),
        ("alice-test@example.com", "alice-test@example.com"),
    ],
)
def test_normalize_rate_limit_identity_collapses_known_alias_conventions(raw, expected):
    from thenetwork.security.rate_limit import normalize_rate_limit_identity

    assert normalize_rate_limit_identity(raw) == expected


def test_plus_addressed_senders_share_a_rate_limit_bucket():
    from thenetwork.security.rate_limit import check_rate_limit

    limiter = FakeLimiter()

    with (
        patch("thenetwork.security.rate_limit.get_settings", return_value=_settings()),
        patch(
            "thenetwork.security.rate_limit._get_limiter",
            return_value=(limiter, HealthyStorage()),
        ),
    ):
        assert check_rate_limit("alice+one@gmail.com", sender_authenticated=False)
        assert check_rate_limit("alice+two@gmail.com", sender_authenticated=False)

    sender_hits = [
        key for key in limiter.hit_keys if key.startswith("unauthenticated-sender:")
    ]
    assert sender_hits == [
        "unauthenticated-sender:alice@gmail.com",
        "unauthenticated-sender:alice@gmail.com",
    ]


def test_unauthenticated_sender_uses_smaller_separate_bucket():
    from thenetwork.security.rate_limit import check_rate_limit

    limiter = FakeLimiter()

    with (
        patch("thenetwork.security.rate_limit.get_settings", return_value=_settings()),
        patch(
            "thenetwork.security.rate_limit._get_limiter",
            return_value=(limiter, HealthyStorage()),
        ),
    ):
        assert check_rate_limit("real@example.com", sender_authenticated=False)

    assert limiter.tested[0] == (
        "6 per 1 hour",
        "unauthenticated-sender:real@example.com",
    )
    assert "authenticated-sender:real@example.com" not in limiter.hit_keys


def test_global_cap_blocks_without_consuming_sender_bucket():
    from thenetwork.security.rate_limit import check_rate_limit

    limiter = FakeLimiter(test_results={"global:emails-processed": False})

    with (
        patch("thenetwork.security.rate_limit.get_settings", return_value=_settings()),
        patch(
            "thenetwork.security.rate_limit._get_limiter",
            return_value=(limiter, HealthyStorage()),
        ),
    ):
        assert (
            check_rate_limit("person@example.com", sender_authenticated=True) is False
        )

    assert limiter.hit_keys == []


def test_proactive_rate_limit_skips_sender_bucket_but_keeps_global_cap():
    from thenetwork.security.rate_limit import check_rate_limit

    limiter = FakeLimiter()

    with (
        patch("thenetwork.security.rate_limit.get_settings", return_value=_settings()),
        patch(
            "thenetwork.security.rate_limit._get_limiter",
            return_value=(limiter, HealthyStorage()),
        ),
    ):
        assert check_rate_limit(
            "person@example.com",
            sender_authenticated=True,
            skip_sender_limit=True,
        )

    assert limiter.tested == [("200 per 1 hour", "global:emails-processed")]
    assert limiter.hit_keys == ["global:emails-processed"]


def test_rate_limit_fails_closed_when_storage_unhealthy():
    from thenetwork.security.rate_limit import check_rate_limit

    storage = SimpleNamespace(check=lambda: False)

    with (
        patch("thenetwork.security.rate_limit.get_settings", return_value=_settings()),
        patch(
            "thenetwork.security.rate_limit._get_limiter",
            return_value=(FakeLimiter(), storage),
        ),
    ):
        assert (
            check_rate_limit("person@example.com", sender_authenticated=True) is False
        )


@pytest.mark.integration
def test_postgres_rate_limit_state_survives_limiter_rebuild(pg_engine, monkeypatch):
    import thenetwork.db.session as sess_mod
    import thenetwork.security.rate_limit as rate_limit
    from thenetwork.settings import Settings

    monkeypatch.setattr(sess_mod, "_engine", pg_engine)
    monkeypatch.setattr(
        rate_limit,
        "get_settings",
        lambda: Settings(
            agent_model="test:model",
            small_agent_model="test:model",
            embed_model="test:embed",
            rate_limit_per_hour=2,
            unauthenticated_rate_limit_per_hour=1,
            global_email_rate_limit_per_hour=10,
        ),
    )
    rate_limit._limiter = None
    rate_limit._storage = None

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_limits"))

    assert rate_limit.check_rate_limit("persist@example.com", sender_authenticated=True)
    assert rate_limit.check_rate_limit("persist@example.com", sender_authenticated=True)

    rate_limit._limiter = None
    rate_limit._storage = None

    try:
        assert not rate_limit.check_rate_limit(
            "persist@example.com", sender_authenticated=True
        )
    finally:
        rate_limit._limiter = None
        rate_limit._storage = None


def test_outbound_registration_and_welcome_quotas_fail_closed_when_storage_is_unavailable():
    from thenetwork.agent import tools
    from thenetwork.worker import tasks

    with (
        patch.object(
            tools, "_get_dispatch_limiter", return_value=(UnavailableLimiter(), None)
        ),
        patch.object(
            tools,
            "_get_registration_limiter",
            return_value=(UnavailableLimiter(), None),
        ),
        patch.object(
            tasks, "_get_welcome_limiter", return_value=(UnavailableLimiter(), None)
        ),
    ):
        assert not tools._check_daily_dispatch_cap("dispatch:test", 1)
        assert not tools._hit_registration_quota(
            SimpleNamespace(
                deps=SimpleNamespace(
                    settings=SimpleNamespace(registration_limit_per_day=1)
                )
            )
        )
        assert not tasks._check_welcome_quota("person@example.com")


@pytest.mark.integration
def test_outbound_registration_and_welcome_limiters_use_durable_storage(
    pg_engine, monkeypatch
):
    """All non-inbound quotas share Postgres state across limiter recreation."""
    import thenetwork.db.session as sess_mod
    from thenetwork.agent import tools
    from thenetwork.worker import tasks

    monkeypatch.setattr(sess_mod, "_engine", pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_limits"))

    limit = __import__("limits").parse("1/day")
    cases = [
        (
            tools,
            "_dispatch_limiter",
            "_dispatch_storage",
            tools._get_dispatch_limiter,
            "test:dispatch",
        ),
        (
            tools,
            "_registration_limiter",
            "_registration_storage",
            tools._get_registration_limiter,
            "test:registration",
        ),
        (
            tasks,
            "_welcome_limiter",
            "_welcome_storage",
            tasks._get_welcome_limiter,
            "test:welcome",
        ),
    ]
    for module, limiter_attr, storage_attr, factory, key in cases:
        setattr(module, limiter_attr, None)
        setattr(module, storage_attr, None)
        limiter, _ = factory()
        assert limiter.hit(limit, key)
        setattr(module, limiter_attr, None)
        setattr(module, storage_attr, None)
        rebuilt, _ = factory()
        assert not rebuilt.test(limit, key)
