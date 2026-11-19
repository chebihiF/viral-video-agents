"""Navigation commune du dashboard (#99) : page active, mobile, clavier."""

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
PKG = Path(progress.__file__).parent
STATIC = PKG / "static"
ORDER = [
    ("/", "Accueil"),
    ("/progress", "Suivi"),
    ("/activity", "Activité"),
    ("/interactions", "Interactions"),
    ("/tokens", "Tokens"),
    ("/controls", "Contrôles"),
]
PAGES = [href for href, _ in ORDER]


def unreachable_github(path: str) -> Any:
    raise URLError("réseau coupé")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("VVA_DB_PATH", str(tmp_path / "vva.db"))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    monkeypatch.setattr(progress, "CACHE", progress.GithubCache(unreachable_github))
    auth.LOGIN_LIMITER.reset()
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def navs(html: str) -> dict[str, str]:
    pattern = r'<nav class="(sidenav|tabbar)" aria-label="Navigation principale">(.*?)</nav>'
    found = re.findall(pattern, html, re.S)
    assert {k for k, _ in found} == {"sidenav", "tabbar"}
    return dict(found)


def links(fragment: str) -> list[tuple[str, str, bool]]:
    pattern = r'<a [^>]*?href="([^"]+)"([^>]*)>\s*([^<]+?)\s*</a>'
    anchors = re.findall(pattern, fragment)
    return [(href, label, "aria-current" in attrs) for href, attrs, label in anchors]


@pytest.mark.parametrize("path", PAGES)
def test_sidenav_lists_six_entries_in_order_with_active_page(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    entries = links(navs(resp.text)["sidenav"])
    assert [(h, label) for h, label, _ in entries] == ORDER
    assert [h for h, _, current in entries if current] == [path]
    assert 'aria-current="page"' in resp.text


@pytest.mark.parametrize("path", PAGES)
def test_tabbar_has_four_tabs_and_more_sheet(client: TestClient, path: str) -> None:
    tabbar = navs(client.get(path).text)["tabbar"]
    sheet = tabbar.split("<details", 1)
    assert len(sheet) == 2
    assert [label for _, label, _ in links(sheet[0])] == ["Accueil", "Suivi", "Activité", "Tokens"]
    assert "Plus" in sheet[1]
    assert [label for _, label, _ in links(sheet[1])] == ["Interactions", "Contrôles"]


def test_nav_is_identical_on_every_page(client: TestClient) -> None:
    def normalized(path: str) -> str:
        return client.get(path).text.replace(' aria-current="page"', "")

    reference = {k: re.sub(r"\s+", " ", v) for k, v in navs(normalized(PAGES[0])).items()}
    for path in PAGES[1:]:
        got = {k: re.sub(r"\s+", " ", v) for k, v in navs(normalized(path)).items()}
        assert got == reference


@pytest.mark.parametrize("path", PAGES)
def test_skip_link_is_first_and_targets_main(client: TestClient, path: str) -> None:
    html = client.get(path).text
    assert html.index('class="skip-link" href="#contenu"') < html.index("<nav ")
    assert re.search(r'<main[^>]*\sid="contenu"', html)
    assert "Aller au contenu" in html
    assert html.count('id="contenu"') == 1


def test_login_has_no_nav(client: TestClient) -> None:
    html = TestClient(app, base_url="https://testserver").get("/login").text
    assert "<nav" not in html
    assert "skip-link" not in html


def test_css_has_mobile_tabbar_without_overflow_and_touch_targets() -> None:
    css = (STATIC / "components.css").read_text(encoding="utf-8")
    assert "@media (max-width: 899px)" in css
    assert "overflow-x: hidden" in css
    assert "@media (min-width: 900px)" in css
    assert "min-height: var(--size-touch)" in css


def test_home_link_is_active_on_the_home_page(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    entries = links(navs(resp.text)["sidenav"])
    assert [(h, label) for h, label, current in entries if current] == [("/", "Accueil")]
    assert 'aria-current="page"' in resp.text


def test_pages_share_one_nav_partial() -> None:
    for name in ("home", "progress", "activity", "tokens", "controls", "interactions"):
        text = (PKG / "templates" / f"{name}.html").read_text(encoding="utf-8")
        assert text.count('include "_nav.html"') == 1
