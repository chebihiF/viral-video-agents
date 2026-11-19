"""Accueil, redirections et navigation commune (#58)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, progress
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
TEMPLATES = Path(progress.__file__).parent / "templates"


def unreachable_github(path: str) -> Any:
    raise URLError("réseau coupé")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VVA_DB_PATH", str(tmp_path / "vva.db"))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    monkeypatch.setattr(progress, "CACHE", progress.GithubCache(unreachable_github))
    auth.LOGIN_LIMITER.reset()


def new_client() -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


@pytest.fixture
def client(env: None) -> TestClient:
    c = new_client()
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def test_healthz() -> None:
    resp = new_client().get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --- Accueil -----------------------------------------------------------------------------
def test_home_redirects_visitor_to_login(env: None) -> None:
    resp = new_client().get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_home_is_a_page_for_the_logged_in_parent(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<h1>Accueil</h1>" in resp.text


def test_home_is_closed_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(auth.PASSWORD_HASH_ENV, raising=False)
    resp = new_client().get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_home_is_not_cached(client: TestClient) -> None:
    assert client.get("/").headers["cache-control"] == "no-store"
    assert new_client().get("/").headers["cache-control"] == "no-store"


# --- Navigation commune ------------------------------------------------------------------
def test_nav_is_not_shown_on_login(env: None) -> None:
    assert "<nav" not in new_client().get("/login").text


def test_pages_share_one_nav_partial() -> None:
    for name in ("progress", "activity", "tokens", "controls", "interactions"):
        assert 'include "_nav.html"' in (TEMPLATES / f"{name}.html").read_text()
    assert not (TEMPLATES / "index.html").exists()
