"""Liste d'activité des agents (#5) : 100 derniers événements, vues tableau + flux."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
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


# Historique filtrable (#94)
def ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def add(agent: str, ticket: int | None, start_min: float, end_min: float, **kw: Any) -> None:
    """Un agent travaille sur un ticket de `start_min` à `end_min` minutes dans le passé."""
    hooks = kw.get("hooks", ["PreToolUse", "SubagentStop" if kw.get("stop") else "PostToolUse"])
    stamps = [ago(start_min), ago(end_min)]
    rows = [
        (f"h-{agent}-{ticket}-{i}-{start_min}", ts, hook, "s", agent, f"id-{agent}", "Bash", ticket)
        for i, (ts, hook) in enumerate(zip(stamps, hooks, strict=True))
    ]
    with db.connect() as conn:
        db.insert_events(conn, rows)


def history(html: str) -> str:
    return html.split('id="history"', 1)[1].split("</section>", 1)[0]


def history_rows(html: str) -> list[str]:
    return re.findall(r'<tr class="history-row[^"]*"[^>]*>.*?</tr>', history(html), re.S)


def test_history_shows_agent_ticket_status_duration_in_words(client: TestClient) -> None:
    add("qa", 94, 60, 40, stop=True)
    text = history(client.get("/activity").text)
    for expected in ("qa", "ticket #94", "terminé", "a duré 20 min"):
        assert expected in text, expected
    assert "Historique" in text


def test_filter_by_agent_keeps_only_that_agent(client: TestClient) -> None:
    add("qa", 94, 60, 40, stop=True)
    add("backend", 95, 60, 30, stop=True)
    rows = history_rows(client.get("/activity?agent=qa").text)
    assert len(rows) == 1 and "qa" in rows[0] and "backend" not in rows[0]


def test_filter_by_status_and_active_filter_is_displayed(client: TestClient) -> None:
    add("qa", 94, 60, 40, stop=True)
    add("backend", 95, 1, 0.5)
    html = client.get("/activity?statut=actif").text
    rows = history_rows(html)
    assert len(rows) == 1 and "backend" in rows[0]
    assert "Filtres actifs" in html and "statut : actif" in html


def test_filter_by_minimum_duration(client: TestClient) -> None:
    add("qa", 94, 60, 40, stop=True)  # 20 min
    add("backend", 95, 60, 55, stop=True)  # 5 min
    html = client.get("/activity?duree_min=10").text
    rows = history_rows(html)
    assert len(rows) == 1 and "qa" in rows[0]
    assert "au moins 10 min" in html


def test_filter_without_result_offers_to_clear_it(client: TestClient) -> None:
    add("qa", 94, 60, 40, stop=True)
    html = client.get("/activity?agent=inconnu").text
    assert "Aucun résultat pour ce filtre" in history(html)
    assert '<a href="/activity">Effacer le filtre</a>' in history(html)
    assert history_rows(html) == []


def test_invalid_filters_are_ignored(client: TestClient) -> None:
    add("qa", 94, 60, 40, stop=True)
    resp = client.get("/activity?statut=bidon&duree_min=abc")
    assert resp.status_code == 200 and len(history_rows(resp.text)) == 1


def test_errors_are_highlighted_with_a_count(client: TestClient) -> None:
    add("qa", 94, 60, 40, hooks=["PreToolUse", "PostToolUseFailure"])
    add("backend", 95, 60, 30, stop=True)
    rows = history_rows(client.get("/activity").text)
    failed = next(r for r in rows if "qa" in r)
    assert "history-row--error" in failed and "1 erreur" in failed
    assert "history-row--error" not in next(r for r in rows if "backend" in r)


def test_filter_by_error_status(client: TestClient) -> None:
    add("qa", 94, 60, 40, hooks=["PreToolUse", "PostToolUseFailure"])
    add("backend", 95, 60, 30, stop=True)
    rows = history_rows(client.get("/activity?statut=erreur").text)
    assert len(rows) == 1 and "qa" in rows[0]


def test_history_shows_real_tokens_with_unit(client: TestClient) -> None:
    add("qa", 94, 60, 40, stop=True)
    with db.connect() as conn:
        db.insert_token_usage(
            conn,
            [
                ("p1", ago(50), "s", "id-qa", "sonnet", "input", 1200),
                ("p2", ago(30), "s", "id-qa", "sonnet", "input", 9999),  # hors de la période
            ],
        )
    assert "1 200 jetons" in history(client.get("/activity").text)


def test_history_values_are_escaped_and_agent_form_is_labelled(client: TestClient) -> None:
    add("<b>x</b>", 1, 60, 40, stop=True)
    html = client.get("/activity?agent=<i>").text
    assert "<b>x</b>" not in html and "<i>" not in history(html)
    assert re.search(r'<label for="f-agent">', html)


def test_history_is_bounded_to_the_last_7_days(client: TestClient) -> None:
    add("old", 1, 60 * 24 * 8, 60 * 24 * 8 - 5, stop=True)
    add("qa", 94, 60, 40, stop=True)
    rows = history_rows(client.get("/activity").text)
    assert len(rows) == 1 and "qa" in rows[0]


def test_history_unreadable_database_is_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: object, **k: object) -> list[object]:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(activity, "load_history", broken)
    seed(1)
    assert client.get("/activity").status_code == 503
