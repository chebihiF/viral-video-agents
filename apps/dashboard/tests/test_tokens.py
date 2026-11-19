"""Page /tokens : totaux par agent et par modèle sur 24 h et 7 jours (#7)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, db, tokens, trend
from dashboard.main import app

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    auth.LOGIN_LIMITER.reset()
    return path


def logged_in() -> TestClient:
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def test_tokens_page_requires_login(db_path: Path) -> None:
    resp = TestClient(app, base_url="https://testserver", follow_redirects=False).get("/tokens")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def seed(rows: list[tuple[timedelta, str | None, str, str, int]]) -> None:
    """(âge, agent, modèle, type, nombre) → lignes token_usage."""
    with db.connect() as conn:
        db.insert_token_usage(
            conn,
            [
                (
                    f"p{i}",
                    (NOW - age).isoformat(timespec="microseconds"),
                    "sess-1",
                    agent,
                    model,
                    type_,
                    count,
                )
                for i, (age, agent, model, type_, count) in enumerate(rows)
            ],
        )


def as_dict(totals: list[tokens.Total]) -> dict[str, tuple[int, int]]:
    return {t.label: (t.last_24h, t.last_7d) for t in totals}


H = timedelta(hours=1)
D = timedelta(days=1)


def test_totals_by_agent_and_model_over_24h_and_7d(db_path: Path) -> None:
    seed(
        [
            (H, "backend", "opus", "input", 100),
            (H, "backend", "opus", "output", 20),
            (2 * D, "backend", "sonnet", "cacheRead", 1000),
            (3 * H, None, "sonnet", "input", 5),
            (8 * D, "backend", "opus", "input", 99999),
        ]
    )
    with db.connect() as conn:
        report = tokens.totals(conn, NOW)
    assert as_dict(report.by_agent) == {"backend": (120, 1120), "(fil principal)": (5, 5)}
    assert as_dict(report.by_model) == {"opus": (120, 120), "sonnet": (5, 1005)}


def test_totals_are_sorted_by_7d_descending(db_path: Path) -> None:
    seed([(H, "qa", "haiku", "input", 1), (H, "backend", "opus", "input", 50)])
    with db.connect() as conn:
        report = tokens.totals(conn, NOW)
    assert [t.label for t in report.by_agent] == ["backend", "qa"]


def test_tokens_page_shows_totals(db_path: Path) -> None:
    now = datetime.now(UTC)
    with db.connect() as conn:
        db.insert_token_usage(
            conn,
            [
                (
                    "a",
                    (now - H).isoformat(timespec="microseconds"),
                    "sess-secret",
                    "backend",
                    "opus",
                    "input",
                    1234567,
                ),
                (
                    "b",
                    (now - H).isoformat(timespec="microseconds"),
                    "sess-secret",
                    None,
                    "haiku",
                    "output",
                    42,
                ),
            ],
        )
    resp = logged_in().get("/tokens")
    assert resp.status_code == 200
    text = resp.text
    for expected in (
        "Tokens",
        "Par agent",
        "Par modèle",
        "24 h",
        "7 j",
        "backend",
        "(fil principal)",
    ):
        assert expected in text
    assert "opus" in text and "haiku" in text
    assert "1 234 567" in text
    assert "sess-secret" not in text


def test_tokens_page_empty_state(db_path: Path) -> None:
    resp = logged_in().get("/tokens")
    assert resp.status_code == 200
    assert "Aucune donnée" in resp.text


# Cas limites (QA)
def test_window_boundaries(db_path: Path) -> None:
    def ts(age: timedelta) -> str:
        return (NOW - age).isoformat(timespec="microseconds")  # format réel de otlp.py

    rows = [("a", ts(D), 1), ("b", ts(7 * D), 2), ("c", ts(7 * D + H), 4)]
    with db.connect() as conn:
        db.insert_token_usage(conn, [(k, t, "s", "a", "m", "input", n) for k, t, n in rows])
        report = tokens.totals(conn, NOW)
    assert as_dict(report.by_agent) == {"a": (1, 3)}


def test_agent_and_model_names_are_escaped_arabic_ok(db_path: Path) -> None:
    now = datetime.now(UTC)
    with db.connect() as conn:
        db.insert_token_usage(
            conn,
            [
                (
                    "x",
                    (now - H).isoformat(timespec="microseconds"),
                    "s",
                    "<script>alert(1)</script>",
                    "نموذج",
                    "input",
                    7,
                ),
            ],
        )
    text = logged_in().get("/tokens").text
    assert "<script>alert(1)" not in text
    assert "نموذج" in text


def test_tokens_page_excludes_old_rows(db_path: Path) -> None:
    now = datetime.now(UTC)
    with db.connect() as conn:
        db.insert_token_usage(
            conn,
            [
                (
                    "o",
                    (now - 9 * D).isoformat(timespec="microseconds"),
                    "s",
                    "old-agent",
                    "m",
                    "input",
                    5,
                )
            ],
        )
    text = logged_in().get("/tokens").text
    assert "old-agent" not in text


def test_agent_named_session_stays_distinct_from_main_thread(db_path: Path) -> None:
    seed([(H, "session", "opus", "input", 7), (H, None, "opus", "input", 3)])
    with db.connect() as conn:
        report = tokens.totals(conn, NOW)
    assert as_dict(report.by_agent) == {"session": (7, 7), "(fil principal)": (3, 3)}


# Tendance, plafond (#96)
def daily(rows: list[tuple[timedelta, int]]) -> list[trend.Day]:
    seed([(age, "a", "m", "input", n) for age, n in rows])
    with db.connect() as conn:
        return trend.daily_usage(conn, NOW)


def test_daily_usage_has_seven_utc_days_oldest_first(db_path: Path) -> None:
    days = daily([(H, 100), (D, 40), (3 * D, 7), (7 * D, 999)])
    assert [d.day for d in days] == [f"2026-11-{n}" for n in range(12, 19)]
    assert [d.tokens for d in days] == [0, 0, 0, 7, 0, 40, 100]
    assert days[-1].partial and not days[0].partial


def test_daily_usage_empty_days_are_zero(db_path: Path) -> None:
    with db.connect() as conn:
        assert [d.tokens for d in trend.daily_usage(conn, NOW)] == [0] * 7


def test_variation_vs_previous_day(db_path: Path) -> None:
    days = daily([(H, 150), (D, 100), (2 * D, 200)])
    assert days[-1].change == "+50 %" and days[-1].delta == 50
    assert days[-2].change == "-50 %"
    assert days[0].change is None  # pas de veille dans le tableau


def test_variation_from_zero_is_not_a_percentage(db_path: Path) -> None:
    days = daily([(H, 10)])
    assert days[-1].change == "nouveau (veille sans token)"
    assert days[-2].change == "0 %" or days[-2].change is None


def test_cap_margin_in_words() -> None:
    one_thousand = tokens.thousands(1000)
    assert trend.cap_text(cap=1000, used=250) == (
        "marge restante : 750 tokens avant le plafond "
        f"(250 sur {one_thousand} tokens, 24 dernières heures)"
    )
    assert trend.cap_text(cap=1000, used=1000).startswith("plafond atteint")
    assert tokens.thousands(1500) in trend.cap_text(cap=2_000, used=500)


def test_tokens_page_shows_trend_cap_and_no_cost(db_path: Path) -> None:
    now = datetime.now(UTC)
    with db.connect() as conn:
        db.insert_token_usage(
            conn, [("t", (now - H).isoformat(timespec="microseconds"), "s", "a", "m", "input", 300)]
        )
        db.set_cap(conn, 1000)
    text = logged_in().get("/tokens").text
    assert "Tokens par jour" in text
    assert "variation vs la veille" in text.lower()
    assert "marge restante : 700 tokens avant le plafond" in text
    assert "€" not in text  # aucune grille de prix locale : pas de coût inventé


def test_tokens_page_trend_has_seven_rows(db_path: Path) -> None:
    seed([(H, "a", "m", "input", 5)])
    text = logged_in().get("/tokens").text
    body = text.split('data-testid="daily"')[1].split("</table>")[0]
    assert body.count("<tr") == 8  # en-tête + 7 jours


def test_tokens_page_unreadable_database_is_503(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = logged_in()

    def boom(*_a: object, **_k: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(tokens, "totals", boom)
    resp = client.get("/tokens")
    assert resp.status_code == 503
    assert "indisponible" in resp.text.lower()
