"""Accueil, redirections et navigation commune (#58)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, progress
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
TEMPLATES = Path(progress.__file__).parent / "templates"
LABELS = {
    "/progress": "Suivi",
    "/activity": "Activité",
    "/tokens": "Tokens",
    "/controls": "Contrôles",
}


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
def nav_links(html: str) -> list[tuple[str, str, bool]]:
    nav = re.search(r'<nav aria-label="Navigation principale">(.*?)</nav>', html, re.S)
    assert nav, "barre de navigation absente"
    anchors = re.findall(r"<a href=\"([^\"]+)\"([^>]*)>([^<]+)</a>", nav.group(1))
    return [(href, label, 'aria-current="page"' in attrs) for href, attrs, label in anchors]


@pytest.mark.parametrize("path", list(LABELS))
def test_page_has_common_nav_with_active_link(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    links = nav_links(resp.text)
    assert [(href, label) for href, label, _ in links[:4]] == list(LABELS.items())
    assert [href for href, _, current in links if current] == [path]


def test_interactions_page_keeps_the_nav_without_active_main_link(client: TestClient) -> None:
    links = nav_links(client.get("/interactions").text)
    assert [href for href, _, current in links if current] == ["/interactions"]


def test_nav_is_not_shown_on_login(env: None) -> None:
    assert "<nav" not in new_client().get("/login").text


def test_pages_share_one_nav_partial() -> None:
    for name in ("progress", "activity", "tokens", "controls", "interactions"):
        assert 'include "_nav.html"' in (TEMPLATES / f"{name}.html").read_text()
    assert not (TEMPLATES / "index.html").exists()
