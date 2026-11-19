"""États vides parlants du dashboard (#98) : titre, explication, action suivante."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, db, progress
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
PAGES = ["/activity", "/interactions", "/tokens"]
STATE = re.compile(r'<section class="state"[^>]*>(.*?)</section>', re.S)


class FakeGithub:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.issues: list[dict[str, Any]] = []

    def __call__(self, path: str) -> Any:
        if self.error:
            raise self.error
        return self.issues if "/issues" in path else []


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGithub:
    fake = FakeGithub()
    monkeypatch.setattr(progress, "CACHE", progress.GithubCache(fake))
    return fake


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: FakeGithub) -> TestClient:
    monkeypatch.setenv("VVA_DB_PATH", str(tmp_path / "vva.db"))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    auth.LOGIN_LIMITER.reset()
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def seed_everything() -> None:
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    with db.connect() as conn:
        db.insert_events(conn, [("e1", now, "PreToolUse", "s1", "qa", "ag-1", "Bash", None)])
        db.insert_token_usage(conn, [("t1", now, "s1", "ag-1", "sonnet", "input", 10)])


# Scénario : base vide
@pytest.mark.parametrize("path", PAGES)
def test_empty_database_explains_and_gives_next_action(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    (state,) = STATE.findall(resp.text)
    assert re.search(r"<h2[^>]*>[^<]+</h2>", state), "titre"
    assert re.search(r"<p[^>]*>[^<]{20,}</p>", state), "phrase d'explication"
    assert 'class="state-action"' in state, "action suivante"
    assert "<svg" in state and 'aria-hidden="true"' in state


def test_activity_points_to_the_ingest_contract(client: TestClient) -> None:
    (state,) = STATE.findall(client.get("/activity").text)
    assert "télémétrie" in state
    assert "ingest-api" in state


def test_controls_with_empty_database_shows_state_and_keeps_controls(client: TestClient) -> None:
    resp = client.get("/controls")
    assert resp.status_code == 200
    assert len(STATE.findall(resp.text)) == 1
    assert 'action="/controls/pause"' in resp.text  # le parent garde la main sans données


# Scénario : GitHub injoignable
def test_progress_github_down_replaces_the_lists(client: TestClient, gh: FakeGithub) -> None:
    gh.error = URLError("réseau coupé")
    resp = client.get("/progress")
    assert resp.status_code == 200
    (state,) = STATE.findall(resp.text)
    assert "indisponible, réessai dans 60 s" in state
    assert "Aucun ticket" not in resp.text
    assert 'href="/progress"' in state  # Réessayer


def test_progress_github_up_but_no_ticket_keeps_the_neutral_state(client: TestClient) -> None:
    resp = client.get("/progress")
    assert "indisponible" not in resp.text
    assert "Aucun ticket" in resp.text


# Scénario : des données
def test_no_empty_state_when_data_exists(client: TestClient, gh: FakeGithub) -> None:
    seed_everything()
    gh.issues = [
        {"number": 2, "title": "EPIC: x", "state": "open", "state_reason": None,
         "body": "", "labels": [{"name": "epic"}]},
        {"number": 3, "title": "T3", "state": "open", "state_reason": None,
         "body": "Épique : #2", "labels": [{"name": "in-progress"}]},
    ]  # fmt: skip
    for path in [*PAGES, "/controls", "/progress"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert 'class="state"' not in resp.text, path
    assert "Aucun agent n'a encore travaillé" not in client.get("/activity").text
    assert "Aucune donnée" not in client.get("/tokens").text
