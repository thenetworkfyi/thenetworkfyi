from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text


class FakeLimiter:
    def __init__(self, *, test_result: bool = True, hit_result: bool = True) -> None:
        self.test_result = test_result
        self.hit_result = hit_result
        self.tested: list[tuple[str, str]] = []
        self.hits: list[tuple[str, str, int]] = []

    def test(self, limit, key: str, cost: int = 1) -> bool:
        self.tested.append((str(limit), key))
        return self.test_result

    def hit(self, limit, key: str, cost: int = 1) -> bool:
        self.hits.append((str(limit), key, cost))
        return self.hit_result


class UnavailableLimiter:
    def test(self, limit, key: str, cost: int = 1) -> bool:
        raise RuntimeError("token-budget storage unavailable")

    def hit(self, limit, key: str, cost: int = 1) -> bool:
        raise RuntimeError("token-budget storage unavailable")


def test_check_daily_token_budget_disabled_when_cap_not_positive():
    from thenetwork.security.token_budget import check_daily_token_budget

    limiter = FakeLimiter(test_result=False)
    with patch(
        "thenetwork.security.token_budget._get_limiter",
        return_value=(limiter, None),
    ):
        assert check_daily_token_budget(0) is True
        assert check_daily_token_budget(-5) is True
    assert limiter.tested == []


def test_check_daily_token_budget_reflects_limiter_test():
    from thenetwork.security.token_budget import check_daily_token_budget

    limiter = FakeLimiter(test_result=False)
    with patch(
        "thenetwork.security.token_budget._get_limiter",
        return_value=(limiter, None),
    ):
        assert check_daily_token_budget(1_000) is False
    assert limiter.tested == [("1000 per 1 day", "daily-agent-token-budget")]


def test_check_daily_token_budget_fails_closed_on_storage_error():
    from thenetwork.security.token_budget import check_daily_token_budget

    with patch(
        "thenetwork.security.token_budget._get_limiter",
        return_value=(UnavailableLimiter(), None),
    ):
        assert check_daily_token_budget(1_000) is False


def test_consume_daily_token_budget_disabled_when_cap_or_tokens_not_positive():
    from thenetwork.security.token_budget import consume_daily_token_budget

    limiter = FakeLimiter()
    with patch(
        "thenetwork.security.token_budget._get_limiter",
        return_value=(limiter, None),
    ):
        assert consume_daily_token_budget(100, 0) is True
        assert consume_daily_token_budget(0, 1_000) is True
    assert limiter.hits == []


def test_consume_daily_token_budget_debits_atomically_with_cost():
    from thenetwork.security.token_budget import consume_daily_token_budget

    limiter = FakeLimiter(hit_result=True)
    with patch(
        "thenetwork.security.token_budget._get_limiter",
        return_value=(limiter, None),
    ):
        assert consume_daily_token_budget(2_500, 15_000_000) is True
    assert limiter.hits == [("15000000 per 1 day", "daily-agent-token-budget", 2_500)]


def test_consume_daily_token_budget_reports_exhaustion_without_raising():
    from thenetwork.security.token_budget import consume_daily_token_budget

    limiter = FakeLimiter(hit_result=False)
    with patch(
        "thenetwork.security.token_budget._get_limiter",
        return_value=(limiter, None),
    ):
        assert consume_daily_token_budget(999_999, 1) is False


def test_consume_daily_token_budget_fails_closed_on_storage_error():
    from thenetwork.security.token_budget import consume_daily_token_budget

    with patch(
        "thenetwork.security.token_budget._get_limiter",
        return_value=(UnavailableLimiter(), None),
    ):
        assert consume_daily_token_budget(100, 1_000) is False


@pytest.mark.integration
def test_daily_agent_token_cap_is_settings_configurable(pg_engine, monkeypatch):
    """The cap must be read fresh per call, not captured at import/definition
    time - sim runs and tests mutate `get_settings()` in place at runtime."""
    import thenetwork.db.session as sess_mod
    import thenetwork.security.token_budget as token_budget

    monkeypatch.setattr(sess_mod, "_engine", pg_engine)
    token_budget._limiter = None
    token_budget._storage = None

    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_limits"))

    small_cap = 10
    assert token_budget.consume_daily_token_budget(4, small_cap) is True
    assert token_budget.consume_daily_token_budget(4, small_cap) is True
    assert token_budget.check_daily_token_budget(small_cap) is True
    # The third consume pushes the running total (12) past the 10-token cap.
    assert token_budget.consume_daily_token_budget(4, small_cap) is False
    assert token_budget.check_daily_token_budget(small_cap) is False

    token_budget._limiter = None
    token_budget._storage = None
