"""Liste d'activité des agents (#5) : 100 derniers événements, vues tableau + flux."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dashboard import activity, auth, db
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
TEMPLATES = Path(activity.__file__).parent / "templates"


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


def seed(n: int, **extra: Any) -> None:
    rows = [
        (
            f"evt-{i:03d}",
            f"2026-11-18T10:{i // 60:02d}:{i % 60:02d}+00:00",
            "PreToolUse",
            "sess-1",
            extra.get("agent_type", "backend"),
            extra.get("agent_id", f"agent-{i % 3}"),
            extra.get("tool_name", "Bash"),
            None,
        )
        for i in range(n)
    ]
    with db.connect() as conn:
        db.insert_events(conn, rows)


def feed_ids(html: str) -> list[str]:
    return re.findall(r'data-event-id="([^"]+)"', html)


# Scénario : liste
def test_shows_100_latest_events_newest_first(client: TestClient) -> None:
    seed(150)
    resp = client.get("/activity")
    assert resp.status_code == 200
    ids = feed_ids(resp.text)
    assert ids == [f"evt-{i:03d}" for i in range(149, 49, -1)]


def test_each_event_shows_time_agent_type_id_event_and_tool(client: TestClient) -> None:
    seed(1, agent_type="qa", agent_id="agent-42", tool_name="Read")
    html = client.get("/activity").text
    assert '<time datetime="2026-11-18T10:00:00+00:00">' in html
    for value in ("qa", "agent-42", "PreToolUse", "Read"):
        assert value in html, value


def test_latest_events_query_is_bounded() -> None:
    assert activity.MAX_EVENTS == 100


def test_agents_table_lists_each_agent_once(client: TestClient) -> None:
    seed(10)
    html = client.get("/activity").text
    table = html.split("<table", 1)[1].split("</table>", 1)[0]
    assert 'scope="col"' in table
    for agent in ("agent-0", "agent-1", "agent-2"):
        assert table.count(f">{agent}<") == 1, agent


def test_values_are_html_escaped(client: TestClient) -> None:
    seed(1, tool_name="<script>alert(1)</script>")
    html = client.get("/activity").text
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_missing_optional_fields_show_dash(client: TestClient) -> None:
    seed(1, agent_type=None, agent_id=None, tool_name=None)
    resp = client.get("/activity")
    assert resp.status_code == 200
    assert "—" in resp.text


# Structure sémantique (docs/design/activity.md)
def test_semantic_structure(client: TestClient) -> None:
    seed(3)
    html = client.get("/activity").text
    assert "<main" in html and "<h1>Activité des agents</h1>" in html
    assert re.search(r'<section[^>]* aria-labelledby="[^"]+"', html)
    assert re.search(r'<aside[^>]* aria-labelledby="[^"]+"', html)
    assert '<ol aria-live="polite"' in html
    assert "<caption" in html


def test_page_not_cached(client: TestClient) -> None:
    assert "no-store" in client.get("/activity").headers["cache-control"]


# États
def test_empty_state(client: TestClient) -> None:
    html = client.get("/activity").text
    assert "Aucun agent n'a encore travaillé" in html
    assert feed_ids(html) == []


def test_error_state_when_database_unreadable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(conn: sqlite3.Connection, limit: int = 100) -> list[activity.Event]:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(activity, "latest_events", broken)
    resp = client.get("/activity")
    assert resp.status_code == 503
    assert 'role="alert"' in resp.text
    assert "Réessayer" in resp.text
    assert "disk I/O" not in resp.text


def test_anonymous_request_never_reads_events(
    db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(activity, "latest_events", lambda *a, **k: calls.append(1) or [])
    anon = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert anon.get("/activity").status_code == 303
    assert calls == []


# Gabarits : tokens.css uniquement, aucune ressource distante
@pytest.mark.parametrize("name", ["login.html", "activity.html"])
def test_templates_use_only_design_tokens(name: str) -> None:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/static/tokens.css">' in text
    assert not re.search(r"https?://", text), "aucun CDN ni ressource distante"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", text), "couleurs en dur"
    style = "".join(re.findall(r"<style>(.*?)</style>", text, re.S))
    assert not re.search(r"font-family:(?!\s*var\()", style), "polices via variables"


def test_static_tokens_served(client: TestClient) -> None:
    resp = client.get("/static/tokens.css")
    assert resp.status_code == 200
    assert "--color-bg" in resp.text
