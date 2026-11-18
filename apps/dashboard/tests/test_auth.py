"""Connexion au dashboard (#5) : mot de passe, cookie de session signé, limitation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import auth
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
SESSION_KEY = "k" * 48
CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "dashboard-routes.md"
NEUTRAL = "Identifiants invalides."


@pytest.fixture(autouse=True)
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VVA_DB_PATH", str(tmp_path / "vva.db"))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, SESSION_KEY)
    auth.LOGIN_LIMITER.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def login(client: TestClient, password: str = PASSWORD) -> int:
    return client.post("/login", data={"password": password}).status_code


# Scénario : accès protégé
def test_activity_redirects_to_login_when_anonymous(client: TestClient) -> None:
    resp = client.get("/activity")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert "<table" not in resp.text


@pytest.mark.parametrize(
    "cookie",
    [
        "",
        "n'importe-quoi",
        "9999999999.AAAA",
        auth.sign_session(SESSION_KEY, expires_at=1),
        "9" * 5000 + ".x",
    ],
    ids=["vide", "illisible", "signature-fausse", "expire", "trop-long"],
)
def test_forged_or_expired_cookie_redirects(client: TestClient, cookie: str) -> None:
    client.cookies.set(auth.COOKIE_NAME, cookie)
    assert client.get("/activity").status_code == 303


def test_expired_signed_cookie_is_rejected() -> None:
    token = auth.sign_session(SESSION_KEY, expires_at=1)
    assert not auth.verify_session(SESSION_KEY, token, now=2)
    assert auth.verify_session(SESSION_KEY, token, now=0)


def test_cookie_signed_with_other_key_is_rejected() -> None:
    token = auth.sign_session("autre-cle-" * 5, expires_at=10)
    assert not auth.verify_session(SESSION_KEY, token, now=0)


def test_login_page_is_public(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'type="password"' in resp.text


# Scénario : connexion
def test_good_password_sets_signed_cookie_and_shows_activity(client: TestClient) -> None:
    resp = client.post("/login", data={"password": PASSWORD})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/activity"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE_NAME}=")
    for attr in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attr.lower() in cookie.lower(), attr
    value = cookie.split(";")[0].split("=", 1)[1]
    assert auth.verify_session(SESSION_KEY, value)
    assert client.get("/activity").status_code == 200


# Scénario : mauvais mot de passe
@pytest.mark.parametrize("password", ["faux", "", PASSWORD + " "], ids=["faux", "vide", "espace"])
def test_wrong_password_is_neutral_401(client: TestClient, password: str) -> None:
    resp = client.post("/login", data={"password": password})
    assert resp.status_code == 401
    assert NEUTRAL in resp.text
    assert "set-cookie" not in resp.headers


def test_missing_field_is_neutral_401(client: TestClient) -> None:
    resp = client.post("/login", data={"autre": "x"})
    assert resp.status_code == 401
    assert NEUTRAL in resp.text


def test_oversized_body_is_rejected(client: TestClient) -> None:
    resp = client.post("/login", data={"password": "x" * (auth.MAX_FORM_BYTES + 1)})
    assert resp.status_code == 413


def test_login_limited_to_5_attempts_per_minute(client: TestClient) -> None:
    for _ in range(5):
        assert login(client, "faux") == 401
    resp = client.post("/login", data={"password": PASSWORD})
    assert resp.status_code == 429
    assert "retry-after" in resp.headers
    assert "set-cookie" not in resp.headers


def test_limit_window_slides_after_a_minute() -> None:
    now = [1_000.0]
    limiter = auth.RateLimiter(max_attempts=5, window_s=60, clock=lambda: now[0])
    assert all(limiter.allow() for _ in range(5))
    assert not limiter.allow()
    now[0] += 60.1
    assert limiter.allow()


def test_password_compared_via_pbkdf2() -> None:
    encoded = auth.hash_password(PASSWORD, iterations=1_000)
    assert encoded.startswith("pbkdf2_sha256:1000:")
    assert PASSWORD not in encoded
    assert auth.verify_password(PASSWORD, encoded)
    assert not auth.verify_password("faux", encoded)


@pytest.mark.parametrize(
    "encoded",
    ["", "md5:1:aa:bb", "pbkdf2_sha256:x:aa:bb", "pbkdf2_sha256:0:aa:bb", "pbkdf2_sha256:1:zz:bb"],
)
def test_malformed_hash_never_matches(encoded: str) -> None:
    assert not auth.verify_password(PASSWORD, encoded)


@pytest.mark.parametrize("env", [auth.PASSWORD_HASH_ENV, auth.SESSION_KEY_ENV])
def test_login_closed_when_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.delenv(env)
    assert login(client) == 503


def test_short_session_key_closes_login(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "court")
    assert login(client) == 503


# Scénario : /healthz reste public et sans donnée
def test_healthz_public_and_without_data(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert set(resp.json()) == {"status", "version"}


# Contrat : contracts/dashboard-routes.md documente routes, variables et cookie
def test_contract_documents_routes_env_and_cookie() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    for name in (auth.PASSWORD_HASH_ENV, auth.SESSION_KEY_ENV, auth.COOKIE_NAME):
        assert f"`{name}`" in text, name
    documented = set(re.findall(r"`(?:GET|POST) (/[a-z/]*)`", text))
    served = set(app.openapi()["paths"])
    assert {"/login", "/activity", "/healthz"} <= documented <= served


def test_non_ascii_or_huge_expiry_is_rejected_without_error() -> None:
    assert not auth.verify_session(SESSION_KEY, "\u00b2.x", now=0)
    assert not auth.verify_session(SESSION_KEY, "9" * 5000 + ".x", now=0)


def test_darija_arabic_password_is_neutral_401(client: TestClient) -> None:
    resp = client.post("/login", data={"password": "كلمة_السر_غلط"})
    assert resp.status_code == 401
    assert NEUTRAL in resp.text
    assert "set-cookie" not in resp.headers
