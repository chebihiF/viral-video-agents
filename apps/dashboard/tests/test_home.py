"""Accueil (#93) : vue d'ensemble agents, tickets, tokens, fil de délégations."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, db, home, progress
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "dashboard-routes.md"


def gh_issue(number: int, *, closed: bool = False, labels: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Ticket {number}",
        "state": "closed" if closed else "open",
        "state_reason": "completed" if closed else None,
        "body": "Épique : #2",
        "labels": [{"name": name} for name in labels],
    }


ISSUES = [
    gh_issue(2, labels=("epic",)),
    gh_issue(3, closed=True),
    gh_issue(4, closed=True),
    gh_issue(5, labels=("P1",)),
    gh_issue(6, labels=("in-progress",)),
]


def github_ok(path: str) -> Any:
    return ISSUES if "/issues" in path else []


def github_down(path: str) -> Any:
    raise URLError("réseau coupé")


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    monkeypatch.setattr(progress, "CACHE", progress.GithubCache(github_ok))
    auth.LOGIN_LIMITER.reset()
    return path


def anonymous() -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


@pytest.fixture
def client() -> TestClient:
    c = anonymous()
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def put_event(
    n: int,
    ago: timedelta,
    *,
    hook: str = "PreToolUse",
    agent: str = "dev-backend",
    agent_id: str | None = "a1",
    tool: str | None = None,
) -> None:
    ts = (datetime.now(UTC) - ago).isoformat()
    with db.connect() as conn, conn:
        conn.execute(
            "INSERT INTO events (event_id, ts, hook_event_name, session_id, agent_type, agent_id,"
            " tool_name) VALUES (?, ?, ?, 's1', ?, ?, ?)",
            (f"e{n}", ts, hook, agent, agent_id, tool),
        )


def put_tokens(n: int, ago: timedelta, count: int) -> None:
    with db.connect() as conn:
        ts = (datetime.now(UTC) - ago).isoformat(timespec="microseconds")
        db.insert_token_usage(conn, [(f"p{n}", ts, "s1", "a1", "opus", "input", count)])


def text(html: str) -> str:
    """Texte visible : sans feuilles de style ni balises, espaces fines normalisées."""
    body = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).replace(" ", " ")


def card(html: str, title: str) -> str:
    match = re.search(
        rf'<section[^>]*aria-labelledby="([^"]+)"[^>]*>(?:(?!</section>).)*?'
        rf">{re.escape(title)}</h2>.*?</section>",
        html,
        re.S,
    )
    assert match, f"carte « {title} » absente"
    return match.group(0)


# --- Accès -------------------------------------------------------------------------------
def test_no_session_redirects_to_login() -> None:
    resp = anonymous().get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_home_is_a_page_not_a_redirect_with_session(client: TestClient) -> None:
    put_event(1, timedelta(seconds=10))
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert "<h1>Accueil</h1>" in resp.text


# --- Vue d'ensemble ----------------------------------------------------------------------
def test_overview_shows_agents_tickets_tokens_and_last_event(client: TestClient) -> None:
    put_event(1, timedelta(minutes=30), agent="qa", agent_id="a2", hook="SubagentStop")
    put_event(2, timedelta(seconds=20), tool="Bash")
    put_tokens(1, timedelta(hours=2), 1_200)
    put_tokens(2, timedelta(days=3), 8_000)
    page = client.get("/").text
    agents = text(card(page, "Agents"))
    assert "1 agent actif" in agents
    assert "agents en marche" in agents.lower()
    assert "dev-backend" in agents and "PreToolUse" in agents  # dernier événement
    tickets = text(card(page, "Tickets"))
    for label, count in (("Prêt", 1), ("En cours", 1), ("Revue", 0), ("Livré", 2)):
        assert f"{label} {count}" in tickets
    tokens = text(card(page, "Tokens"))
    assert "1 200 tokens sur les dernières 24 h" in tokens
    assert "9 200 tokens sur les 7 derniers jours" in tokens


def test_every_card_links_to_its_detail_page(client: TestClient) -> None:
    put_event(1, timedelta(seconds=5))
    page = client.get("/").text
    for title, href in (
        ("Agents", "/activity"),
        ("Tickets", "/progress"),
        ("Tokens", "/tokens"),
        ("Fil des délégations", "/interactions"),
    ):
        assert f'href="{href}"' in card(page, title), title


def test_page_is_live(client: TestClient) -> None:
    put_event(1, timedelta(seconds=5))
    page = client.get("/").text
    assert "data-stream" in page and 'data-refresh="#home"' in page and 'id="home"' in page
    assert "/static/live.js" in page


def test_delegation_feed_lists_latest_delegations(client: TestClient) -> None:
    put_event(1, timedelta(seconds=40), agent="tech-lead-x", agent_id=None, tool="Task")
    put_event(2, timedelta(seconds=30), agent="qa", hook="SubagentStart", agent_id="a9")
    feed = text(card(client.get("/").text, "Fil des délégations"))
    assert "tech lead → qa" in feed


def test_paused_agents_are_labelled(client: TestClient) -> None:
    put_event(1, timedelta(seconds=5))
    with db.connect() as conn:
        db.pause(conn, "manuel")
    assert "agents en pause" in text(card(client.get("/").text, "Agents")).lower()


# --- Plafond et quota --------------------------------------------------------------------
def test_cap_reached_shows_alert_badge(client: TestClient) -> None:
    put_event(1, timedelta(seconds=5))
    put_tokens(1, timedelta(hours=1), 600_000)
    page = client.get("/").text
    badge = re.search(r'<[^>]*class="badge badge--danger"[^>]*>(.*?)</', page, re.S)
    assert badge, "badge d'alerte absent"
    assert "Plafond de tokens atteint" in badge.group(1)


def test_no_alert_below_the_cap(client: TestClient) -> None:
    put_event(1, timedelta(seconds=5))
    put_tokens(1, timedelta(hours=1), 10)
    assert "Plafond de tokens atteint" not in client.get("/").text


def test_without_quota_view_raw_tokens_and_help_never_a_percent(client: TestClient) -> None:
    put_event(1, timedelta(seconds=5))
    put_tokens(1, timedelta(hours=1), 1_000)
    page = client.get("/").text
    visible = text(page)
    assert "%" not in visible
    assert "Quota non configuré" in visible
    assert "tokens bruts" in visible


# --- Dégradations ------------------------------------------------------------------------
def test_github_unreachable_marks_tickets_unavailable_others_stay(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(progress, "CACHE", progress.GithubCache(github_down))
    put_event(1, timedelta(seconds=5))
    put_tokens(1, timedelta(hours=1), 500)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "indisponible" in text(card(resp.text, "Tickets"))
    assert "500 tokens" in text(card(resp.text, "Tokens"))
    assert "1 agent actif" in text(card(resp.text, "Agents"))


def test_empty_database_shows_empty_state(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Aucun agent n'a encore travaillé" in text(resp.text)
    assert 'class="state"' in resp.text
    assert 'id="home"' in resp.text  # la page se complète en direct dès le premier événement


def test_unreadable_database_is_a_503_with_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_now: datetime) -> home.Home:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(home, "collect", boom)
    resp = client.get("/")
    assert resp.status_code == 503
    assert 'role="alert"' in resp.text and 'href="/"' in resp.text


# --- Contrat -----------------------------------------------------------------------------
def test_contract_documents_home_route() -> None:
    row = next(
        line
        for line in CONTRACT.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `GET /` |")
    )
    assert "200" in row and "/login" in row and "/progress" not in row
