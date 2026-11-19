"""« Qui fait quoi » en direct (#101) : agents, ticket, état, durée sur /activity."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, db, live
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    auth.LOGIN_LIMITER.reset()
    return path


@pytest.fixture
def client(db_file: Path) -> TestClient:
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def add_ticket_column() -> None:
    """Simule le champ `ticket` de #100 (absent du schéma tant que #100 n'est pas livré)."""
    with db.connect() as conn, conn:
        conn.execute("ALTER TABLE events ADD COLUMN ticket INTEGER")


def put(
    n: int,
    ago: timedelta,
    *,
    agent: str = "dev-backend",
    hook: str = "PreToolUse",
    ticket: int | None = 93,
    now: datetime = NOW,
) -> None:
    ts = (now - ago).isoformat()
    with db.connect() as conn, conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
        sql = "INSERT INTO events (event_id, ts, hook_event_name, session_id, agent_type, agent_id"
        values: list[object] = [f"evt-{n}", ts, hook, "sess-1", agent, f"id-{agent}"]
        if "ticket" in cols:
            sql += ", ticket"
            values.append(ticket)
        conn.execute(sql + f") VALUES ({', '.join('?' * len(values))})", values)


def rows_at(now: datetime = NOW) -> list[live.LiveRow]:
    with db.connect() as conn:
        return live.agent_rows(conn, now)


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)


# Scénario : agent actif sur un ticket
def test_active_agent_shows_ticket_state_and_duration(db_file: Path) -> None:
    add_ticket_column()
    put(1, minutes(12))
    put(2, timedelta(seconds=30))
    [row] = rows_at()
    assert (row.agent, row.ticket, row.state, row.duration) == (
        "dev-backend",
        93,
        "actif",
        "depuis 12 min",
    )


def test_page_shows_the_line(client: TestClient) -> None:
    add_ticket_column()
    now = datetime.now(UTC)
    put(1, minutes(12) + timedelta(seconds=5), now=now)
    put(2, timedelta(seconds=30), now=now)
    html = client.get("/activity").text
    assert "dev-backend · ticket #93 · actif · depuis 12 min" in text_of(html)


# Scénario : états selon l'ancienneté du dernier événement
@pytest.mark.parametrize(
    ("ago", "state"),
    [
        (timedelta(seconds=119), "actif"),
        (minutes(2), "en attente"),
        (minutes(15), "en attente"),
        (timedelta(minutes=15, seconds=1), "terminé"),
        (minutes(20), "terminé"),
    ],
)
def test_state_thresholds(db_file: Path, ago: timedelta, state: str) -> None:
    put(1, ago + minutes(5))
    put(2, ago)
    assert rows_at()[0].state == state


def test_subagent_stop_is_finished_even_when_recent(db_file: Path) -> None:
    put(1, minutes(5))
    put(2, timedelta(seconds=10), hook="SubagentStop")
    assert rows_at()[0].state == "terminé"


def test_events_after_a_stop_make_agent_active_again(db_file: Path) -> None:
    put(1, minutes(5), hook="SubagentStop")
    put(2, timedelta(seconds=10))
    assert rows_at()[0].state == "actif"


def test_idle_agent_shows_last_event_time(client: TestClient) -> None:
    now = datetime.now(UTC)
    put(1, minutes(30), now=now)
    put(2, minutes(20), now=now)
    html = client.get("/activity").text
    last = (now - minutes(20)).strftime("%H:%M")
    assert "terminé" in text_of(html)
    assert f"dernier événement à {last} UTC" in text_of(html)


def test_waiting_agent_shows_last_event_time(db_file: Path) -> None:
    put(1, minutes(10))
    put(2, minutes(5))
    [row] = rows_at()
    assert (row.state, row.last_label) == ("en attente", "11:55")


# Scénario : ticket inconnu
def test_event_without_ticket_column_is_unknown_ticket(client: TestClient) -> None:
    put(1, timedelta(seconds=5), now=datetime.now(UTC))
    html = text_of(client.get("/activity").text)
    assert "dev-backend · ticket inconnu · actif" in html


def test_event_with_null_ticket_is_unknown_ticket(db_file: Path) -> None:
    add_ticket_column()
    put(1, timedelta(seconds=5), ticket=None)
    assert rows_at()[0].ticket is None


# Durée : depuis le premier événement de l'agent sur ce ticket
@pytest.mark.parametrize("raw", ["#93", "93a", ""])
def test_non_numeric_ticket_is_unknown_ticket(db_file: Path, raw: str) -> None:
    add_ticket_column()
    put(1, timedelta(seconds=5), ticket=raw)  # type: ignore[arg-type]
    assert rows_at()[0].ticket is None


def test_events_older_than_the_window_are_ignored(db_file: Path) -> None:
    put(1, live.HISTORY_WINDOW + minutes(1), agent="ancien")
    put(2, live.HISTORY_WINDOW - minutes(1), agent="recent")
    assert [r.agent for r in rows_at()] == ["recent"]


def test_agent_on_two_tickets_gives_two_rows(db_file: Path) -> None:
    add_ticket_column()
    put(1, minutes(60), ticket=90)
    put(2, minutes(50), ticket=90)
    put(3, minutes(10), ticket=93)
    rows = rows_at()
    assert [(r.ticket, r.state, r.duration) for r in rows] == [
        (93, "en attente", "depuis 10 min"),
        (90, "terminé", "a duré 10 min"),
    ]


@pytest.mark.parametrize(
    ("delta", "label"),
    [
        (timedelta(seconds=20), "moins d'une minute"),
        (minutes(1), "1 min"),
        (minutes(65), "1 h 05"),
        (timedelta(hours=26), "1 j 2 h"),
    ],
)
def test_duration_labels(delta: timedelta, label: str) -> None:
    assert live.format_duration(delta) == label


def test_most_recent_rows_first_and_capped(db_file: Path) -> None:
    for i in range(live.MAX_ROWS + 5):
        put(i, timedelta(seconds=i + 1), agent=f"agent-{i}")
    rows = rows_at()
    assert len(rows) == live.MAX_ROWS
    assert rows[0].agent == "agent-0"


def test_bad_timestamp_is_ignored(db_file: Path) -> None:
    with db.connect() as conn, conn:
        conn.execute(
            "INSERT INTO events (event_id, ts, hook_event_name, session_id) "
            "VALUES ('x', 'pas-une-date', 'PreToolUse', 's')"
        )
    assert rows_at() == []


def test_unnamed_agent_has_a_label(db_file: Path) -> None:
    with db.connect() as conn, conn:
        conn.execute(
            "INSERT INTO events (event_id, ts, hook_event_name, session_id) "
            "VALUES ('x', ?, 'PreToolUse', 's')",
            ((NOW - timedelta(seconds=5)).isoformat(),),
        )
    assert rows_at()[0].agent == "agent inconnu"


# Sécurité : échappement, aucune donnée sensible
def test_agent_name_is_escaped(client: TestClient) -> None:
    put(1, timedelta(seconds=5), agent="<script>alert(1)</script>", now=datetime.now(UTC))
    html = client.get("/activity").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# Scénarios : accès et états
def test_no_session_redirects_to_login(db_file: Path) -> None:
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    r = c.get("/activity")
    assert (r.status_code, r.headers["location"]) == (303, "/login")


def test_empty_database_shows_empty_state_without_live_block(client: TestClient) -> None:
    r = client.get("/activity")
    assert r.status_code == 200
    assert "Aucun agent n'a encore travaillé" in unescape(r.text)
    assert "Qui fait quoi" not in r.text


def test_database_error_is_still_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_now: datetime) -> list[live.LiveRow]:
        raise sqlite3.OperationalError("disque plein")

    put(1, timedelta(seconds=5), now=datetime.now(UTC))
    monkeypatch.setattr(live, "load_rows", boom)
    r = client.get("/activity")
    assert r.status_code == 503
    assert "Qui fait quoi" not in r.text


# Temps réel : le bloc est dans la zone rafraîchie par le flux existant
def test_live_block_is_inside_refreshed_zone(client: TestClient) -> None:
    put(1, timedelta(seconds=5), now=datetime.now(UTC))
    html = client.get("/activity").text
    assert 'data-refresh="#agents"' in html
    zone = html.split('<div id="agents">', 1)[1]
    assert "Qui fait quoi" in zone
    assert zone.index("Qui fait quoi") < zone.index('id="agents-table"')
