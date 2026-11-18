"""Interactions entre agents (#6) : sessions, sous-agents (SubagentStart/Stop) et durée."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, db, interactions
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
TEMPLATES = Path(interactions.__file__).parent / "templates"
Row = tuple[str, str, str, str, str | None, str | None, str | None]


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


def ev(
    eid: str,
    ts: str,
    name: str,
    session: str = "sess-1",
    agent_id: str | None = None,
    agent_type: str | None = None,
) -> Row:
    return (eid, f"2026-11-18T{ts}+00:00", name, session, agent_type, agent_id, None)


def seed(*rows: Row) -> None:
    with db.connect() as conn:
        db.insert_events(conn, rows)


def sub(n: int, start: str, stop: str | None, session: str = "sess-1") -> list[Row]:
    rows = [ev(f"s{n}-a", start, "SubagentStart", session, f"sub-{n}", "backend")]
    if stop:
        rows.append(ev(f"s{n}-b", stop, "SubagentStop", session, f"sub-{n}", "backend"))
    return rows


# Scénario : interactions
def test_shows_session_its_subagent_and_duration(client: TestClient) -> None:
    seed(ev("e0", "10:00:00", "SessionStart"), *sub(1, "10:00:05", "10:02:07"))
    resp = client.get("/interactions")
    assert resp.status_code == 200
    html = resp.text
    for value in ("sess-1", "sub-1", "backend", "2 min 02 s"):
        assert value in html, value


def test_running_subagent_has_no_duration(client: TestClient) -> None:
    seed(*sub(1, "10:00:05", None))
    html = client.get("/interactions").text
    assert "En cours" in html
    assert "min" not in html.split("<tbody", 1)[1]


def test_only_same_agent_id_pairs_up(client: TestClient) -> None:
    seed(*sub(1, "10:00:00", "10:00:10"), *sub(2, "10:00:02", "10:00:30"))
    html = client.get("/interactions").text
    assert "10 s" in html and "28 s" in html


def test_subagent_in_other_session_is_not_mixed(client: TestClient) -> None:
    seed(*sub(1, "10:00:00", "10:00:10", "sess-A"), *sub(2, "10:00:00", None, "sess-B"))
    with db.connect() as conn:
        sessions = {s.session_id: s for s in interactions.load_sessions(conn)}
    assert [s.agent_id for s in sessions["sess-A"].subagents] == ["sub-1"]
    assert sessions["sess-A"].subagents[0].duration_s == 10
    assert sessions["sess-B"].subagents[0].duration_s is None


def test_stop_without_start_has_unknown_duration(client: TestClient) -> None:
    seed(ev("x", "10:00:09", "SubagentStop", "sess-1", "sub-9", "qa"))
    html = client.get("/interactions").text
    assert "sub-9" in html and "—" in html


def test_sessions_newest_first_and_bounded(client: TestClient) -> None:
    seed(*(ev(f"e{i}", f"10:{i:02d}:00", "PreToolUse", f"sess-{i:02d}") for i in range(30)))
    html = client.get("/interactions").text
    found = re.findall(r"<code>(sess-\d\d)</code>", html)
    assert found[0] == "sess-29" and len(found) == interactions.MAX_SESSIONS == 20


def test_values_are_html_escaped(client: TestClient) -> None:
    seed(ev("e", "10:00:00", "SubagentStart", "<script>x</script>", "<b>", "<i>"))
    html = client.get("/interactions").text
    assert "<script>x" not in html and "&lt;script&gt;" in html


@pytest.mark.parametrize(
    ("seconds", "label"),
    [(0.4, "< 1 s"), (12, "12 s"), (125, "2 min 05 s"), (3725, "1 h 02 min")],
)
def test_duration_label(seconds: float, label: str) -> None:
    t0 = datetime(2026, 11, 18, tzinfo=UTC).isoformat()
    t1 = datetime.fromtimestamp(datetime(2026, 11, 18, tzinfo=UTC).timestamp() + seconds, UTC)
    s = interactions.Subagent("a", None, t0, t1.isoformat())
    assert s.duration_label == label


# Accès et états
def test_requires_session(db_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(interactions, "load_sessions", lambda *a, **k: calls.append(1) or [])
    anon = TestClient(app, base_url="https://testserver", follow_redirects=False)
    resp = anon.get("/interactions")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert calls == []


def test_empty_state(client: TestClient) -> None:
    html = client.get("/interactions").text
    assert "Aucune interaction" in html
    assert 'id="sessions"' in html


def test_error_state_when_database_unreadable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(conn: sqlite3.Connection) -> list[interactions.Session]:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(interactions, "load_sessions", broken)
    resp = client.get("/interactions")
    assert resp.status_code == 503
    assert 'role="alert"' in resp.text and "Réessayer" in resp.text
    assert "disk I/O" not in resp.text


def test_page_not_cached_and_live_wired(client: TestClient) -> None:
    seed(*sub(1, "10:00:00", None))
    resp = client.get("/interactions")
    assert "no-store" in resp.headers["cache-control"]
    assert 'data-last-id="1"' in resp.text and 'data-refresh="#sessions"' in resp.text
    assert '<script src="/static/live.js" defer></script>' in resp.text


def test_semantic_structure(client: TestClient) -> None:
    seed(*sub(1, "10:00:00", "10:00:03"))
    html = client.get("/interactions").text
    assert "<main" in html and "<h1>Interactions entre agents</h1>" in html
    assert "<caption" in html and 'scope="col"' in html


def test_template_uses_only_design_tokens() -> None:
    text = (TEMPLATES / "interactions.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/static/tokens.css">' in text
    assert not re.search(r"https?://", text)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", text), "couleurs en dur"


def test_contract_documents_interactions() -> None:
    text = (
        Path(interactions.__file__).parents[4] / "contracts" / "dashboard-routes.md"
    ).read_text()
    for needle in ("/interactions", "SubagentStart", "SubagentStop", "20 dernières sessions"):
        assert needle in text, needle
