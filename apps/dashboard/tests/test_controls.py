"""Contrôles pause et plafond de tokens (#8) : /controls, POST protégés (CSRF), /api/v1/control."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, controls, db, tokens
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
INGEST = "jeton-de-test-pas-un-secret"  # noqa: S105 - jeton factice de test
BEARER = {"Authorization": f"Bearer {INGEST}"}
CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "control-api.md"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv("INGEST_TOKEN", INGEST)
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    auth.LOGIN_LIMITER.reset()
    return path


def new_client() -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def logged_in() -> TestClient:
    c = new_client()
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def csrf_of(client: TestClient) -> str:
    page = client.get("/controls")
    assert page.status_code == 200
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert match, "jeton CSRF absent du formulaire"
    return match.group(1)


def post(client: TestClient, action: str, **fields: str) -> int:
    data = {"csrf": csrf_of(client), **fields}
    return client.post(f"/controls/{action}", data=data).status_code


def state() -> dict[str, object]:
    resp = new_client().get("/api/v1/control", headers=BEARER)
    assert resp.status_code == 200
    body: dict[str, object] = resp.json()
    return body


def seed_tokens(count: int, age: timedelta = timedelta(hours=1)) -> None:
    ts = (datetime.now(UTC) - age).isoformat(timespec="microseconds")
    with db.connect() as conn:
        db.insert_token_usage(conn, [("p1", ts, "s", "backend", "opus", "input", count)])


# --- Accès et état initial ---------------------------------------------------------------
def test_controls_requires_login(db_path: Path) -> None:
    resp = new_client().get("/controls")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert not db_path.exists()  # aucune lecture de la base sans session


def test_initial_state_running_with_default_cap(db_path: Path) -> None:
    body = state()
    assert body["paused"] is False
    assert body["reason"] is None
    assert body["cap_tokens"] == 500_000
    assert body["tokens_24h"] == 0


def test_page_shows_state_and_forms(db_path: Path) -> None:
    resp = logged_in().get("/controls")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert 'action="/controls/pause"' in resp.text
    assert "/static/tokens.css" in resp.text
    assert tokens.thousands(500_000) in resp.text


# --- Scénario : pause / reprise ------------------------------------------------------------
def test_pause_is_stored_and_exposed_to_api(db_path: Path) -> None:
    client = logged_in()
    assert post(client, "pause") == 303
    body = state()
    assert body["paused"] is True
    assert body["reason"] == "manuel"
    page = client.get("/controls").text
    assert 'action="/controls/resume"' in page
    assert "Agents en pause (motif : manuel)" in page


def test_resume_clears_pause(db_path: Path) -> None:
    client = logged_in()
    post(client, "pause")
    assert post(client, "resume") == 303
    body = state()
    assert body["paused"] is False
    assert body["reason"] is None


def test_pause_twice_and_resume_when_running_are_idempotent(db_path: Path) -> None:
    client = logged_in()
    assert post(client, "resume") == 303
    assert post(client, "pause") == 303
    assert post(client, "pause") == 303
    assert state()["paused"] is True


# --- API : jeton Bearer ----------------------------------------------------------------------
def test_api_requires_bearer(db_path: Path) -> None:
    c = new_client()
    assert c.get("/api/v1/control").status_code == 401
    assert c.get("/api/v1/control", headers={"Authorization": "Bearer faux"}).status_code == 401


def test_api_session_cookie_is_not_enough(db_path: Path) -> None:
    assert logged_in().get("/api/v1/control").status_code == 401


def test_api_closed_without_configured_token(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INGEST_TOKEN")
    assert new_client().get("/api/v1/control", headers=BEARER).status_code == 503


def test_api_is_no_store_and_has_documented_keys(db_path: Path) -> None:
    resp = new_client().get("/api/v1/control", headers=BEARER)
    assert resp.headers["cache-control"] == "no-store"
    assert set(resp.json()) == {"paused", "reason", "cap_tokens", "tokens_24h", "updated_at"}


# --- CSRF ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["pause", "resume", "cap"])
def test_post_without_csrf_is_refused(db_path: Path, action: str) -> None:
    client = logged_in()
    resp = client.post(f"/controls/{action}", data={"cap_tokens": "1000"})
    assert resp.status_code == 403
    assert state()["paused"] is False and state()["cap_tokens"] == 500_000


def test_post_with_wrong_csrf_is_refused(db_path: Path) -> None:
    client = logged_in()
    assert client.post("/controls/pause", data={"csrf": "x" * 43}).status_code == 403
    assert state()["paused"] is False


def test_csrf_token_is_bound_to_the_session(db_path: Path) -> None:
    other = logged_in()
    stolen = csrf_of(other)
    session_cookie = auth.sign_session("s" * 48, expires_at=2_000_000_000)
    mine = new_client()
    mine.cookies.set(auth.COOKIE_NAME, session_cookie)
    assert csrf_of(mine) != stolen
    assert mine.post("/controls/pause", data={"csrf": stolen}).status_code == 403
    assert state()["paused"] is False


def test_csrf_token_changes_with_key_and_cookie() -> None:
    a = controls.csrf_token("k" * 32, "cookie-1")
    assert a == controls.csrf_token("k" * 32, "cookie-1")
    assert a != controls.csrf_token("k" * 32, "cookie-2")
    assert a != controls.csrf_token("j" * 32, "cookie-1")


def test_post_without_session_redirects_without_effect(db_path: Path) -> None:
    resp = new_client().post("/controls/pause", data={"csrf": "x"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert not db_path.exists()


def test_post_body_too_large(db_path: Path) -> None:
    client = logged_in()
    resp = client.post("/controls/pause", data={"csrf": "x" * 10_000})
    assert resp.status_code == 413


# --- Scénario : plafond -------------------------------------------------------------------------
def test_cap_reached_pauses_with_reason_and_banner(db_path: Path) -> None:
    seed_tokens(500_000)
    body = state()
    assert body["paused"] is True
    assert body["reason"] == "plafond"
    assert body["tokens_24h"] == 500_000
    page = logged_in().get("/controls").text
    assert "Plafond atteint" in page
    assert 'role="alert"' in page


def test_below_cap_does_not_pause(db_path: Path) -> None:
    seed_tokens(499_999)
    assert state()["paused"] is False


def test_tokens_older_than_24h_do_not_count(db_path: Path) -> None:
    seed_tokens(900_000, age=timedelta(days=2))
    assert state()["paused"] is False


def test_manual_pause_keeps_its_reason_over_cap(db_path: Path) -> None:
    client = logged_in()
    post(client, "pause")
    seed_tokens(600_000)
    assert state()["reason"] == "manuel"


def test_resume_refused_while_cap_is_still_reached(db_path: Path) -> None:
    seed_tokens(500_000)
    client = logged_in()
    assert post(client, "resume") == 409
    assert state()["paused"] is True
    assert "Plafond atteint" in client.get("/controls").text


def test_raising_the_cap_then_resume(db_path: Path) -> None:
    seed_tokens(500_000)
    client = logged_in()
    assert post(client, "cap", cap_tokens="800000") == 303
    assert state()["cap_tokens"] == 800_000
    assert post(client, "resume") == 303
    assert state()["paused"] is False


@pytest.mark.parametrize("value", ["", "abc", "0", "-5", "1.5", "99999999999999", "٣"])
def test_invalid_cap_is_refused(db_path: Path, value: str) -> None:
    client = logged_in()
    assert post(client, "cap", cap_tokens=value) == 422
    assert state()["cap_tokens"] == 500_000


def test_lowering_the_cap_below_usage_pauses(db_path: Path) -> None:
    seed_tokens(10_000)
    client = logged_in()
    assert post(client, "cap", cap_tokens="5000") == 303
    body = state()
    assert body["paused"] is True and body["reason"] == "plafond"


# --- Page utile (#97) : agents, confirmation, quota, historique ----------------------------
def seed_event(
    hook: str, age: timedelta, agent: str = "qa", ticket: int | None = 97, tool: str | None = None
) -> None:
    ts = (datetime.now(UTC) - age).isoformat(timespec="microseconds")
    with db.connect() as conn:
        db.insert_events(conn, [(f"e-{hook}-{age}", ts, hook, "s1", agent, "ag-1", tool, ticket)])


def section(page: str, title: str) -> str:
    match = re.search(rf"<h2[^>]*>{title}</h2>(.*?)(?=<h2|</main>)", page, re.S)
    assert match, title
    return match.group(1)


def test_agents_listed_with_ticket_and_state(db_path: Path) -> None:
    seed_event("PreToolUse", timedelta(seconds=10), "qa", 97)
    seed_event("PreToolUse", timedelta(minutes=5), "reviewer", None)
    agents = section(logged_in().get("/controls").text, "Agents")
    pattern = r"qa</strong> ·\s*<span>ticket #97</span> ·\s*<span[^>]*>[^<]*actif"
    assert re.search(pattern, agents)
    assert "reviewer" in agents and "ticket inconnu" in agents and "en attente" in agents


def test_agents_show_paused_state_when_paused(db_path: Path) -> None:
    seed_event("PreToolUse", timedelta(seconds=10))
    client = logged_in()
    post(client, "pause")
    agents = section(client.get("/controls").text, "Agents")
    assert "en pause" in agents and "actif" not in agents


def test_finished_agents_are_not_listed(db_path: Path) -> None:
    seed_event("SubagentStop", timedelta(seconds=10), "devops")
    assert "devops" not in section(logged_in().get("/controls").text, "Agents")


def test_empty_database_shows_empty_state_and_keeps_controls(db_path: Path) -> None:
    page = logged_in().get("/controls").text
    assert 'class="state"' in page
    assert 'action="/controls/pause"' in page and 'action="/controls/cap"' in page


def test_pause_needs_an_explicit_confirmation_first(db_path: Path) -> None:
    page = logged_in().get("/controls").text
    warning = "Les agents s'arrêteront après leur étape en cours"
    assert warning in page
    assert page.index(warning) < page.index('action="/controls/pause"')
    assert "Confirmer la pause" in page


def test_pause_result_is_displayed(db_path: Path) -> None:
    client = logged_in()
    post(client, "pause")
    page = client.get("/controls").text
    assert "Résultat" in page and "Agents en pause (motif : manuel)" in page


def test_quota_is_labelled_with_unit_and_current_value(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VVA_QUOTA_5H_TOKENS", "200000")
    seed_tokens(50_000, age=timedelta(hours=1))
    cap = section(logged_in().get("/controls").text, "Plafond de tokens")
    assert f"{tokens.thousands(200_000)} tokens par 5 h" in cap
    assert f"{tokens.thousands(50_000)}" in cap


def test_quota_not_configured_says_so_without_percent(db_path: Path) -> None:
    cap = section(logged_in().get("/controls").text, "Plafond de tokens")
    assert "Quota par 5 h non configuré" in cap and "VVA_QUOTA_5H_TOKENS" in cap
    assert "%" not in cap


def test_history_lists_date_action_result(db_path: Path) -> None:
    seed_event("SubagentStart", timedelta(minutes=30), "qa", 97)
    seed_event("PostToolUseFailure", timedelta(minutes=20), "qa", 97, tool="Bash")
    seed_event("SubagentStop", timedelta(minutes=10), "qa", 97)
    history = section(logged_in().get("/controls").text, "Historique des actions")
    for head in ("Date", "Action", "Résultat"):
        assert f">{head}</th>" in history
    assert "qa démarre" in history and "lancé" in history
    assert "Bash" in history and "échec" in history
    assert "qa se termine" in history and "terminé" in history
    assert history.index("se termine") < history.index("démarre")  # plus récent en haut


def test_history_without_action_says_so(db_path: Path) -> None:
    history = section(logged_in().get("/controls").text, "Historique des actions")
    assert "Aucune action" in history


# --- Échec sûr (pages fermées) ----------------------------------------------------------------
def test_unreadable_db_is_503_for_page_post_and_api(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = logged_in()
    csrf = csrf_of(client)
    broken = db_path.parent / "dossier.db"
    broken.mkdir()
    monkeypatch.setenv("VVA_DB_PATH", str(broken))
    assert client.get("/controls").status_code == 503
    assert client.post("/controls/pause", data={"csrf": csrf}).status_code == 503
    assert new_client().get("/api/v1/control", headers=BEARER).status_code == 503


# --- Contrat ------------------------------------------------------------------------------------
def test_contract_documents_routes_keys_and_env() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    for route in ("GET /controls", "POST /controls/pause", "GET /api/v1/control"):
        assert f"`{route}`" in text, route
    for key in ("paused", "reason", "cap_tokens", "tokens_24h", "updated_at"):
        assert f"`{key}`" in text, key
    for name in ("VVA_CONTROL_URL", "VVA_INGEST_TOKEN", "PreToolUse", "code 2"):
        assert name in text, name
    served = set(app.openapi()["paths"])
    assert {"/controls", "/controls/pause", "/controls/resume", "/controls/cap"} <= served
    assert "/api/v1/control" in served
