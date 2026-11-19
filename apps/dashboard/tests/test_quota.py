"""Quota de tokens réglable par le parent (#100) : fenêtre de 5 h et semaine, jamais inventé."""

from __future__ import annotations

import pytest

from dashboard import quota

ENV = ("VVA_QUOTA_5H_TOKENS", "VVA_QUOTA_WEEK_TOKENS")


@pytest.fixture(autouse=True)
def no_quota_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


# Scénario : quota absent = « non configuré », sans exception
def test_unset_quota_is_not_configured() -> None:
    assert quota.read_quota() == quota.Quota(five_hours=None, week=None)
    assert not quota.read_quota().configured


def test_quota_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VVA_QUOTA_5H_TOKENS", "1200000")
    monkeypatch.setenv("VVA_QUOTA_WEEK_TOKENS", " 9000000 ")
    assert quota.read_quota() == quota.Quota(five_hours=1_200_000, week=9_000_000)
    assert quota.read_quota().configured


def test_each_window_is_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VVA_QUOTA_WEEK_TOKENS", "9000000")
    assert quota.read_quota() == quota.Quota(five_hours=None, week=9_000_000)


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "1.5", "1e6", "١٢٣"])
def test_invalid_value_is_not_configured(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("VVA_QUOTA_5H_TOKENS", raw)
    assert quota.read_quota().five_hours is None
