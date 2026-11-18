"""Suivi visuel du projet (#26) : /progress et /api/v1/progress. GitHub est toujours mocké."""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from fastapi.testclient import TestClient

from dashboard import activity, auth, db, progress
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = ROOT / "contracts" / "progress.schema.json"
TEMPLATE = Path(progress.__file__).parent / "templates" / "progress.html"
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)


def issue(
    number: int,
    *,
    state: str = "open",
    labels: tuple[str, ...] = (),
    epic: int | None = 2,
    title: str | None = None,
    pr: bool = False,
) -> dict[str, Any]:
    body = f"Épique : #{epic}" if epic else "sans épique"
    item: dict[str, Any] = {
        "number": number,
        "title": title or f"Ticket {number}",
        "state": state,
        "body": body,
        "labels": [{"name": name} for name in labels],
    }
    if pr:
        item["pull_request"] = {}
    return item


def pull(
    number: int, ref: str, *, state: str = "open", merged_at: str | None = None, sha: str = ""
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"feat: PR {number}",
        "state": state,
        "merged_at": merged_at,
        "merge_commit_sha": sha or None,
        "head": {"ref": ref},
    }


ISSUES = [
    issue(2, labels=("epic",), epic=None, title="EPIC: Dashboard"),
    issue(3, state="closed", labels=("P0",)),
    issue(4, labels=("P1",)),  # prêt
    issue(5, labels=("P1",)),  # en revue (PR ouverte)
    issue(6, labels=("P2",)),  # backlog
    issue(7, labels=("P1", "blocked")),  # backlog (bloqué)
    issue(8, labels=("P1", "in-progress")),  # en cours
    issue(9, labels=("decision",), epic=None),  # journal : ignoré
    issue(10, epic=None, pr=True),  # une PR : ignorée
    issue(11, state="closed", labels=("wontfix",)),  # abandonné : ignoré
    issue(12, labels=("P0",), epic=None, title="Sans parent"),  # orphelin, prêt
]
PULLS = [
    pull(40, "feat/5-revue"),
    pull(41, "feat/3-fait", state="closed", merged_at="2026-11-17T09:00:00Z", sha="a" * 40),
    pull(42, "feat/9-autre", state="closed", merged_at="2026-11-18T04:00:00Z", sha="b" * 40),
]
TAGS = [
    {"name": "v0.1.0", "commit": {"sha": "0" * 40}},
    {"name": "v0.2.0", "commit": {"sha": "a" * 40}},
    {"name": "v0.3.0", "commit": {"sha": "b" * 40}},
]


class FakeGithub:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.data: dict[str, Any] = {"issues": ISSUES, "pulls": PULLS, "tags": TAGS}

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        if self.fail:
            raise URLError("réseau coupé")
        return self.data[next(key for key in self.data if f"/{key}" in path)]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGithub:
    fake = FakeGithub()
    clock = Clock()
    fake.clock = clock  # type: ignore[attr-defined]
    monkeypatch.setattr(progress, "CACHE", progress.GithubCache(fake, clock=clock))
    return fake


@pytest.fixture
def db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    auth.LOGIN_LIMITER.reset()
    return path


@pytest.fixture
def client(db_file: Path, gh: FakeGithub) -> TestClient:
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def seed(*events: tuple[str, str, str, str, str | None]) -> None:
    """(ts, hook, agent_type, agent_id, tool)."""
    rows = [
        (f"e{i}", ts, hook, "sess", agent, agent_id, tool)
        for i, (ts, hook, agent, agent_id, tool) in enumerate(events)
    ]
    with db.connect() as conn:
        db.insert_events(conn, rows)


def api(client: TestClient) -> dict[str, Any]:
    resp = client.get("/api/v1/progress")
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    return body


# Scénario : avancement global
def test_epic_counts_and_percent(client: TestClient) -> None:
    epics = {e["id"]: e for e in api(client)["epics"]}
    assert epics[2]["title"] == "EPIC: Dashboard"
    assert epics[2]["counts"] == {
        "delivered": 1,
        "review": 1,
        "in_progress": 1,
        "ready": 1,
        "backlog": 2,
    }
    assert epics[2]["percent"] == 17  # 1 livré sur 6


def test_orphans_grouped_and_noise_ignored(client: TestClient) -> None:
    body = api(client)
    orphans = next(e for e in body["epics"] if e["id"] == 0)
    assert orphans["title"] == "Sans épique"
    assert orphans["counts"]["ready"] == 1
    assert body["totals"]["counts"] == {
        "delivered": 1,
        "review": 1,
        "in_progress": 1,
        "ready": 2,
        "backlog": 2,
    }
    assert body["totals"]["percent"] == 14  # 1 sur 7 : décisions, PR et abandons exclus


def test_empty_epic_is_zero_percent(client: TestClient, gh: FakeGithub) -> None:
    gh.data["issues"] = [issue(2, labels=("epic",), epic=None)]
    body = api(client)
    assert body["epics"][0]["percent"] == 0
    assert body["totals"]["percent"] == 0


def test_sprint_is_coming_soon(client: TestClient) -> None:
    assert api(client)["sprint"] == {"available": False, "title": None}
    html = client.get("/progress").text
    assert "bientôt disponible" in html


def test_page_shows_progress_with_text_alternatives(client: TestClient) -> None:
    html = client.get("/progress").text
    assert "<h1>Suivi du projet</h1>" in html
    assert re.search(r'role="img" aria-label="EPIC: Dashboard : 1 livrés?, 1 en revue', html)
    assert "17 % · 1 livré / 6" in html
    assert "14 %" in html and "1 livré sur 7 tickets" in html
    assert "<circle" in html  # anneau
    for label in ("Livré", "Revue", "En cours", "Prêt", "Backlog"):
        assert label in html, label


# Scénario : travail des agents
def test_active_agent_card(client: TestClient) -> None:
    start = ago(minutes=12)
    seed(
        (start, "SubagentStart", "reviewer", "ag-1", None),
        (ago(seconds=20), "PreToolUse", "reviewer", "ag-1", "Read"),
    )
    (agent,) = api(client)["agents"]
    assert agent["name"] == "reviewer"
    assert agent["model"] == "claude-opus-5-5"
    assert agent["step"] == "review"
    assert agent["issue"] is None
    assert agent["active"] is True
    assert agent["since"] == start
    assert 11 * 60 <= agent["elapsed_s"] <= 13 * 60
    html = client.get("/progress").text
    assert "reviewer" in html and "claude-opus-5-5" in html
    assert "Actif" in html and "Revue" in html


def test_stopped_agent_is_inactive_and_duration_frozen(client: TestClient) -> None:
    seed(
        (ago(minutes=50), "SubagentStart", "qa", "ag-2", None),
        (ago(minutes=40), "SubagentStop", "qa", "ag-2", None),
    )
    (agent,) = api(client)["agents"]
    assert agent["active"] is False
    assert 9 * 60 <= agent["elapsed_s"] <= 11 * 60
    assert "Inactif" in client.get("/progress").text


def test_silent_agent_is_considered_inactive(client: TestClient) -> None:
    seed((ago(hours=3), "PreToolUse", "dev-backend", "ag-3", "Bash"))
    (agent,) = api(client)["agents"]
    assert agent["active"] is False and agent["step"] == "dev"


def test_pull_request_tool_means_pr_step(client: TestClient) -> None:
    seed(
        (ago(minutes=5), "SubagentStart", "dev-backend", "ag-4", None),
        (ago(minutes=1), "PreToolUse", "dev-backend", "ag-4", "mcp__github__create_pull_request"),
    )
    assert api(client)["agents"][0]["step"] == "pr"


def test_unknown_agent_has_no_model_or_step_and_main_session_is_skipped(
    client: TestClient,
) -> None:
    seed(
        (ago(minutes=2), "SubagentStart", "inconnu", "ag-5", None),
        (ago(minutes=1), "PreToolUse", None, None, "Bash"),  # type: ignore[arg-type]
    )
    (agent,) = api(client)["agents"]
    assert agent["model"] is None and agent["step"] is None


def test_agents_sorted_active_first(client: TestClient) -> None:
    seed(
        (ago(minutes=30), "SubagentStop", "qa", "ag-6", None),
        (ago(minutes=1), "SubagentStart", "devops", "ag-7", None),
    )
    assert [a["name"] for a in api(client)["agents"]] == ["devops", "qa"]


def test_empty_agents_state(client: TestClient) -> None:
    assert api(client)["agents"] == []
    assert "Aucun agent actif" in client.get("/progress").text


# Scénario : historique
def test_timeline_newest_first_joins_tags_and_merged_prs(client: TestClient) -> None:
    timeline = api(client)["timeline"]
    assert [t["version"] for t in timeline] == ["v0.3.0", "v0.2.0", "v0.1.0"]
    assert timeline[0] == {
        "version": "v0.3.0",
        "title": "feat: PR 42",
        "pr": 42,
        "date": "2026-11-18T04:00:00Z",
    }
    assert timeline[2]["pr"] is None and timeline[2]["date"] is None
    html = client.get("/progress").text
    assert "<ol" in html and "v0.3.0" in html and "#42" in html


# Cache 60 s et échec GitHub (décision D20)
def test_github_is_cached_for_60_seconds(client: TestClient, gh: FakeGithub) -> None:
    client.get("/api/v1/progress")
    client.get("/progress")
    assert len(gh.calls) == 3  # issues, pulls, tags : une seule lecture
    gh.clock.now += 61  # type: ignore[attr-defined]
    client.get("/api/v1/progress")
    assert len(gh.calls) == 6


def test_github_failure_serves_last_known_data_with_banner(
    client: TestClient, gh: FakeGithub
) -> None:
    assert api(client)["github_error_at"] is None
    gh.fail = True
    gh.clock.now += 61  # type: ignore[attr-defined]
    body = api(client)
    assert body["github_error_at"] is not None
    assert body["totals"]["counts"]["delivered"] == 1  # dernières données connues
    resp = client.get("/progress")
    assert resp.status_code == 200
    assert 'role="alert"' in resp.text and "Réessayer" in resp.text
    assert "réseau coupé" not in resp.text


def test_github_failure_without_data_degrades_cleanly(client: TestClient, gh: FakeGithub) -> None:
    gh.fail = True
    body = api(client)
    assert body["epics"] == [] and body["timeline"] == []
    assert body["github_error_at"] is not None
    assert 'role="alert"' in client.get("/progress").text


def test_failure_is_not_retried_before_ttl(client: TestClient, gh: FakeGithub) -> None:
    gh.fail = True
    client.get("/api/v1/progress")
    first = len(gh.calls)
    client.get("/api/v1/progress")
    assert len(gh.calls) == first


def test_github_values_are_html_escaped(client: TestClient, gh: FakeGithub) -> None:
    gh.data["issues"] = [issue(2, labels=("epic",), epic=None, title="<script>alert(1)</script>")]
    html = client.get("/progress").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


# Accès : session obligatoire, aucune lecture avant
def test_anonymous_is_refused_without_any_read(
    db_file: Path, gh: FakeGithub, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[int] = []
    monkeypatch.setattr(activity, "latest_events", lambda *a, **k: reads.append(1) or [])
    anon = TestClient(app, base_url="https://testserver", follow_redirects=False)
    page = anon.get("/progress")
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert anon.get("/api/v1/progress").status_code == 401
    assert gh.calls == [] and reads == []


def test_responses_not_cached(client: TestClient) -> None:
    assert "no-store" in client.get("/progress").headers["cache-control"]
    assert "no-store" in client.get("/api/v1/progress").headers["cache-control"]


def test_database_error_degrades_agents_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    def broken(*a: object, **k: object) -> list[activity.Event]:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(activity, "latest_events", broken)
    assert api(client)["agents"] == []
    assert client.get("/progress").status_code == 200


# Accessibilité et gabarit
def test_template_uses_tokens_and_no_remote_resource() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/static/tokens.css">' in text
    assert not re.search(r"https?://", text)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", text)
    style = "".join(re.findall(r"<style>(.*?)</style>", text, re.S))
    assert not re.search(r"font-family:(?!\s*var\()", style)


def test_animations_are_disabled_by_reduced_motion() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "@keyframes" in text
    reduced = text.split("prefers-reduced-motion: reduce", 1)[1]
    assert "animation: none" in reduced


def test_semantic_structure(client: TestClient, gh: FakeGithub) -> None:
    html = client.get("/progress").text
    assert "<main" in html and html.count("<h1") == 1
    assert len(re.findall(r'<section[^>]* aria-labelledby="[^"]+"', html)) >= 3
    assert 'aria-label="Avancement global"' in html
    assert "Actualisé toutes les 60 s" in html


def test_static_refresh_script_served(client: TestClient) -> None:
    resp = client.get("/static/progress.js")
    assert resp.status_code == 200 and "fetch(" in resp.text
    assert not re.search(r"https?://", resp.text)


# Contrat
def _mirror(model: dict[str, Any], contract: dict[str, Any]) -> None:
    assert contract["additionalProperties"] is False
    assert model["additionalProperties"] is False
    assert set(model["properties"]) == set(contract["properties"])
    assert set(model.get("required", [])) == set(contract["required"])


def test_model_matches_contract_schema() -> None:
    contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    model = progress.Progress.model_json_schema()
    _mirror(model, contract)
    assert set(model["$defs"]) == set(contract["$defs"])
    for name, definition in contract["$defs"].items():
        _mirror(model["$defs"][name], definition)


def validate(value: Any, schema: dict[str, Any], defs: dict[str, Any]) -> None:
    """Sous-ensemble de JSON Schema suffisant pour le contrat (pas de dépendance ajoutée)."""
    if "$ref" in schema:
        return validate(value, defs[schema["$ref"].rsplit("/", 1)[1]], defs)
    if "enum" in schema:
        assert value in schema["enum"], value
        return None
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    checks: dict[str, Callable[[Any], bool]] = {
        "null": lambda v: v is None,
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    assert any(checks[t](value) for t in types), (value, types)
    if isinstance(value, dict):
        assert set(schema["required"]) <= set(value), set(schema["required"]) - set(value)
        assert set(value) <= set(schema["properties"]), set(value) - set(schema["properties"])
        for key, item in value.items():
            validate(item, schema["properties"][key], defs)
    if isinstance(value, list):
        for item in value:
            validate(item, schema["items"], defs)
    return None


def test_response_validates_against_contract(client: TestClient) -> None:
    seed((ago(minutes=3), "SubagentStart", "qa", "ag-9", None))
    contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    body = api(client)
    assert body["agents"] and body["epics"] and body["timeline"]
    validate(body, contract, contract["$defs"])


def test_routes_documented_in_contract() -> None:
    text = (ROOT / "contracts" / "dashboard-routes.md").read_text(encoding="utf-8")
    assert "GET /progress" in text and "GET /api/v1/progress" in text
    assert "progress.schema.json" in text


# Client HTTP GitHub : sans jeton, conditionnel (ETag), URL fixe
class FakeResponse:
    def __init__(self, payload: Any, etag: str = '"v1"') -> None:
        self._raw = json.dumps(payload).encode()
        self.headers = {"ETag": etag}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._raw


def test_http_client_sends_no_token_and_reuses_etag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []
    answers: list[Any] = [
        FakeResponse([{"a": 1}]),
        HTTPError("u", 304, "Not Modified", Message(), None),
    ]

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> FakeResponse:
        seen.append(req)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]

    monkeypatch.setattr(progress, "urlopen", fake_urlopen)
    http = progress.HttpJson()
    assert http("/repos/o/r/tags") == [{"a": 1}]
    assert http("/repos/o/r/tags") == [{"a": 1}]  # 304 : ne compte pas dans la limite
    assert seen[0].full_url.startswith("https://api.github.com/repos/o/r/tags")
    assert seen[0].get_header("Authorization") is None
    assert seen[1].get_header("If-none-match") == '"v1"'


def test_http_client_propagates_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(req: urllib.request.Request, timeout: float) -> FakeResponse:
        raise HTTPError("u", 403, "rate limit", Message(), None)

    monkeypatch.setattr(progress, "urlopen", refuse)
    with pytest.raises(HTTPError):
        progress.HttpJson()("/repos/o/r/issues")


@pytest.mark.parametrize("value", ["", "a/b/c", "../x", "a b/c", "o/r?x=1"])
def test_repo_setting_is_validated(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VVA_GITHUB_REPO", value)
    assert progress.repo() == progress.DEFAULT_REPO


def test_repo_setting_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VVA_GITHUB_REPO", "octo/cat")
    assert progress.repo() == "octo/cat"
