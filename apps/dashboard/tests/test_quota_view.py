"""Tokens en % du quota (5 h, semaine, week-end), par agent et par ticket (#103)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, db, quota, quota_view
from dashboard.main import app
from dashboard.tokens import thousands

# Mercredi : il y a 4 jours (samedi 14) et 3 jours (dimanche 15) tombent le week-end.
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
H = timedelta(hours=1)
D = timedelta(days=1)
FULL = quota.Quota(five_hours=1_000_000, week=10_000_000)
NONE = quota.Quota(five_hours=None, week=None)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    for name in (quota.ENV_5H, quota.ENV_WEEK):
        monkeypatch.delenv(name, raising=False)
    auth.LOGIN_LIMITER.reset()
    return path


def logged_in() -> TestClient:
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def seed(rows: list[tuple[timedelta, str, str | None, int]], now: datetime = NOW) -> None:
    """(âge, session, agent, nombre) → lignes token_usage."""
    with db.connect() as conn:
        db.insert_token_usage(
            conn,
            [
                (f"p{i}", (now - age).isoformat(timespec="microseconds"), s, a, "opus", "input", n)
                for i, (age, s, a, n) in enumerate(rows)
            ],
        )


def seed_ticket(session: str, ticket: int | None, age: timedelta = H, now: datetime = NOW) -> None:
    with db.connect() as conn:
        db.insert_events(
            conn,
            [
                (
                    f"e-{session}-{ticket}-{age}",
                    (now - age).isoformat(timespec="microseconds"),
                    "PreToolUse",
                    session,
                    None,
                    None,
                    None,
                    ticket,
                )
            ],
        )


def view(q: quota.Quota) -> quota_view.QuotaView:
    with db.connect() as conn:
        return quota_view.build(conn, NOW, q)


# Gherkin 1 : 420 000 tokens sur 5 h, quota 1 000 000 -> « 42 % du quota des 5 dernières heures »
def test_five_hour_percent_with_numbers(db_path: Path) -> None:
    seed([(H, "s", "qa", 420_000), (6 * H, "s", "qa", 999)])
    gauge = view(FULL).five_hours
    assert gauge.used == 420_000 and gauge.percent == 42 and gauge.level == "ok"
    assert gauge.text == (
        "42 % du quota des 5 dernières heures "
        f"({thousands(420_000)} sur {thousands(1_000_000)} tokens)"
    )
    assert gauge.help is None


# Gherkin 2 : % semaine et % week-end, chacun libellé
def test_week_and_weekend_are_labelled(db_path: Path) -> None:
    seed(
        [
            (H, "s", "qa", 1_000_000),  # mercredi
            (4 * D, "s", "qa", 500_000),  # samedi 14
            (3 * D, "s", "qa", 500_000),  # dimanche 15
            (8 * D, "s", "qa", 7_000_000),  # hors fenêtre
        ]
    )
    v = view(FULL)
    assert v.week.used == 2_000_000 and v.week.percent == 20
    assert v.week.text.startswith("20 % du quota de la semaine")
    assert v.weekend.used == 1_000_000 and v.weekend.percent == 10
    assert v.weekend.text.startswith("10 % du quota de la semaine")
    assert "week-end" in v.weekend.text and "samedi et dimanche" in v.weekend.text


def test_levels_green_orange_red(db_path: Path) -> None:
    def level(used: int) -> str | None:
        return quota_view.gauge(used, 1000, "x", "y", "ENV").level

    assert [level(699), level(700), level(900), level(901), level(2000)] == [
        "ok",
        "warn",
        "warn",
        "danger",
        "danger",
    ]
    assert quota_view.gauge(2000, 1000, "x", "y", "ENV").percent == 200


# Gherkin 3 : trois agents, triés du plus gourmand, % du quota 5 h
def test_agents_sorted_with_percent_of_five_hour_quota(db_path: Path) -> None:
    seed(
        [
            (H, "s", "qa", 100_000),
            (H, "s", "backend", 300_000),
            (H, "s", None, 50_000),
            (2 * D, "s", "qa", 9),
        ]
    )
    rows = view(FULL).by_agent
    assert [r.label for r in rows] == ["backend", "qa", "(fil principal)"]
    assert [(r.last_5h.tokens, r.last_5h.percent) for r in rows] == [
        (300_000, 30),
        (100_000, 10),
        (50_000, 5),
    ]
    assert rows[1].last_7d.tokens == 100_009


# Gherkin 4 : deux tickets, tokens sans ticket sous « ticket inconnu »
def test_tickets_with_unknown_group(db_path: Path) -> None:
    seed([(H, "a", "qa", 200_000), (H, "b", "qa", 100_000), (H, "c", "qa", 10_000)])
    seed_ticket("a", 12)
    seed_ticket("b", 7)
    seed_ticket("b", 8, age=timedelta(minutes=5))  # le plus récent gagne
    rows = view(FULL).by_ticket
    assert [(r.label, r.last_5h.tokens, r.last_5h.percent) for r in rows] == [
        ("#12", 200_000, 20),
        ("#8", 100_000, 10),
        ("ticket inconnu", 10_000, 1),
    ]


# Gherkin 5 : quota non configuré -> jamais de %
def test_unconfigured_quota_shows_raw_tokens_and_help(db_path: Path) -> None:
    seed([(H, "s", "qa", 420_000)])
    v = view(NONE)
    assert not v.configured
    assert v.five_hours.percent is None and v.five_hours.level is None
    assert v.five_hours.help == "quota non configuré : renseigner VVA_QUOTA_5H_TOKENS"
    assert v.week.help == "quota non configuré : renseigner VVA_QUOTA_WEEK_TOKENS"
    assert v.weekend.help == v.week.help
    assert v.by_agent[0].last_5h.percent is None and v.by_agent[0].last_5h.tokens == 420_000


def test_partial_quota_only_five_hours(db_path: Path) -> None:
    seed([(H, "s", "qa", 100_000)])
    v = view(quota.Quota(five_hours=1_000_000, week=None))
    assert v.five_hours.percent == 10 and v.week.percent is None and v.week.help
    assert v.by_agent[0].last_7d.percent is None


# Gherkin 6 : accès et état vide
def test_page_requires_login(db_path: Path) -> None:
    resp = TestClient(app, base_url="https://testserver", follow_redirects=False).get("/tokens")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_empty_database_shows_empty_state_without_quota_block(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(quota.ENV_5H, "1000000")
    text = logged_in().get("/tokens").text
    assert "Aucune donnée" in text and "du quota" not in text


def test_page_shows_percent_legend_and_tables(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setenv(quota.ENV_5H, "1000000")
    monkeypatch.setenv(quota.ENV_WEEK, "10000000")
    seed([(H, "sess-secret", "backend", 420_000)], now=now)
    seed_ticket("sess-secret", 103, now=now)
    text = logged_in().get("/tokens").text
    assert "42 % du quota des 5 dernières heures" in text
    for expected in ("Légende", "70 %", "90 %", "Par ticket", "#103", "Par agent", "backend"):
        assert expected in text
    assert "sess-secret" not in text


def test_page_without_quota_shows_help_not_percent(db_path: Path) -> None:
    seed([(H, "s", "backend", 420_000)], now=datetime.now(UTC))
    text = logged_in().get("/tokens").text
    assert "quota non configuré : renseigner VVA_QUOTA_5H_TOKENS" in text
    assert "% du quota" not in text and "420" in text


def test_ticket_and_agent_names_are_escaped(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(quota.ENV_5H, "1000")
    seed([(H, "s", "<script>x</script>", 5)], now=datetime.now(UTC))
    assert "<script>x" not in logged_in().get("/tokens").text
