"""Suivi visuel du projet (#26) : /progress et /api/v1/progress. GitHub est toujours mocké."""

from __future__ import annotations

import http.client
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
    reason: str | None = None,
    body: str | None = None,
    pr: bool = False,
) -> dict[str, Any]:
    if body is None:
        body = f"Épique : #{epic}" if epic else "sans épique"
    if state == "closed" and reason is None:
        reason = "completed"
    item: dict[str, Any] = {
        "number": number,
        "title": title or f"Ticket {number}",
        "state": state,
        "state_reason": reason,
        "body": body,
        "labels": [{"name": name} for name in labels],
    }
    if pr:
        item["pull_request"] = {"merged_at": None}
    return item


ISSUES = [
    issue(2, labels=("epic",), epic=None, title="EPIC: Dashboard"),
    issue(3, state="closed", labels=("P0",)),
    issue(4, labels=("P1",)),  # prêt
    issue(5, labels=("P1",)),  # en revue (PR ouverte « Closes #5 »)
    issue(6, labels=("P2",)),  # backlog
    issue(7, labels=("P1", "blocked")),  # backlog (bloqué)
    issue(8, labels=("P1", "in-progress")),  # en cours
    issue(9, labels=("decision",), epic=None),  # journal : ignoré
    issue(11, state="closed", labels=("wontfix",)),  # abandonné : ignoré
    issue(12, labels=("P0",), epic=None, title="Sans parent"),  # orphelin, prêt
    issue(13, state="closed", reason="not_planned"),  # fermé sans livraison : ignoré
    issue(40, epic=None, pr=True, body="Closes #5", title="feat: revue"),  # PR ouverte
    issue(41, state="closed", epic=None, pr=True, body="Closes #3"),  # PR fusionnée : ignorée
]
COMMITS = [
    {
        "sha": sha * 40,
        "commit": {"message": f"feat: PR {n} (#{n})\n\ncorps", "committer": {"date": date}},
    }
    for sha, n, date in (("a", 41, "2026-11-17T09:00:00Z"), ("b", 42, "2026-11-18T04:00:00Z"))
]
TAGS = [
    {"name": "v0.1.0", "commit": {"sha": "0" * 40}},
    {"name": "v0.2.0", "commit": {"sha": "a" * 40}},
    {"name": "v0.3.0", "commit": {"sha": "b" * 40}},
]


class FakeGithub:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.data: dict[str, Any] = {"issues": ISSUES, "commits": COMMITS, "tags": TAGS}

    def __call__(self, path: str) -> Any:
        self.calls.append(path)
        if self.error:
            raise self.error
        return self.data[next(key for key in self.data if f"/{key}" in path)]

    @property
    def fail(self) -> bool:
        return self.error is not None

    @fail.setter
    def fail(self, value: bool) -> None:
        self.error = URLError("réseau coupé") if value else None


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
    monkeypatch.setattr(progress, "CACHE", progress.GithubCache(fake, clock=clock, wall=clock))
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
        (f"e{i}", ts, hook, "sess", agent, agent_id, tool, None)
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
    assert epics[2]["percent"] == 16  # 1 livré sur 6, arrondi vers le bas


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
    assert "16 % · 1 livré / 6" in html
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
def test_github_is_cached_for_3_minutes(client: TestClient, gh: FakeGithub) -> None:
    client.get("/api/v1/progress")
    client.get("/progress")
    assert len(gh.calls) == 3  # issues, tags, commits : une seule lecture
    gh.clock.now += 120
    client.get("/api/v1/progress")
    assert len(gh.calls) == 3
    gh.clock.now += 181  # type: ignore[attr-defined]
    client.get("/api/v1/progress")
    assert len(gh.calls) == 6


def test_github_failure_serves_last_known_data_with_banner(
    client: TestClient, gh: FakeGithub
) -> None:
    assert api(client)["github_error_at"] is None
    gh.fail = True
    gh.clock.now += 181  # type: ignore[attr-defined]
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


def test_any_failure_is_absorbed_and_not_retried_before_ttl(
    client: TestClient, gh: FakeGithub
) -> None:
    gh.error = http.client.IncompleteRead(b"")  # n'est pas une OSError
    assert api(client)["github_error_at"] is not None
    count = len(gh.calls)
    gh.clock.now += 100  # type: ignore[attr-defined]
    api(client)
    assert len(gh.calls) == count


def test_rate_limit_waits_for_reset_and_keeps_last_data(client: TestClient, gh: FakeGithub) -> None:
    api(client)
    clock: Clock = gh.clock  # type: ignore[attr-defined]
    gh.error = progress.RateLimited(until=clock.now + 181 + 1000)
    clock.now += 181
    body = api(client)
    assert body["github_error_at"] is not None and body["totals"]["counts"]["delivered"] == 1
    count = len(gh.calls)
    clock.now += 900  # TTL dépassé mais réinitialisation GitHub pas encore atteinte
    api(client)
    assert len(gh.calls) == count
    clock.now += 101
    gh.error = None
    assert api(client)["github_error_at"] is None
    assert len(gh.calls) > count


def test_rate_limit_wait_is_capped(gh: FakeGithub) -> None:
    gh.error = progress.RateLimited(until=10**9)
    clock: Clock = gh.clock  # type: ignore[attr-defined]
    cache = progress.GithubCache(gh, clock=clock, wall=clock)
    cache.get()
    gh.error = None
    clock.now += progress.MAX_RATE_WAIT_S + 1
    assert cache.get()[1] is None


def test_closed_not_planned_is_not_delivered(client: TestClient, gh: FakeGithub) -> None:
    gh.data["issues"] = [
        issue(2, labels=("epic",), epic=None),
        issue(3, state="closed", reason="not_planned"),
    ]
    assert api(client)["totals"]["counts"]["delivered"] == 0


def test_percent_rounds_down(client: TestClient, gh: FakeGithub) -> None:
    gh.data["issues"] = [issue(2, labels=("epic",), epic=None)] + [
        issue(n, state="closed") for n in range(3, 10)
    ]
    gh.data["issues"].append(issue(10, labels=("P2",)))
    assert api(client)["epics"][0]["percent"] == 87  # 7/8 = 87,5 : jamais arrondi au pair


def test_agents_cards_active_plus_five_distinct_inactive(client: TestClient) -> None:
    types = ["qa", "reviewer", "devops", "scout", "ux-designer", "ai-engineer", "dev-backend"]
    events = [(ago(minutes=1), "SubagentStart", "qa", "live", None)]
    for i, kind in enumerate(types):  # chaque type : deux instances inactives
        for j in range(2):
            events.append(
                (ago(minutes=40 + i, seconds=j), "SubagentStop", kind, f"{kind}-{j}", None)
            )
    seed(*events)
    agents = api(client)["agents"]
    assert agents[0]["active"] is True and agents[0]["name"] == "qa"
    inactive = [a["name"] for a in agents if not a["active"]]
    assert len(inactive) == 5 and len(set(inactive)) == 5
    assert "qa" not in inactive  # déjà actif


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
    def __init__(self, payload: Any, etag: str = '"v1"', link: str | None = None) -> None:
        self._raw = json.dumps(payload).encode()
        self.headers = {"ETag": etag, **({"Link": link} if link else {})}

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


def _urlopen_from(
    answers: list[Any], seen: list[urllib.request.Request]
) -> Callable[..., FakeResponse]:
    def fake(req: urllib.request.Request, timeout: float) -> FakeResponse:
        seen.append(req)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]

    return fake


def _next(n: int) -> str:
    return f'<https://api.github.com/repos/o/r/issues?page={n}>; rel="next", <x>; rel="last"'


def test_http_client_follows_next_pages_up_to_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []
    answers = [FakeResponse([n], link=_next(n + 1)) for n in range(1, 10)]
    monkeypatch.setattr(progress, "urlopen", _urlopen_from(answers, seen))
    assert progress.HttpJson()("/repos/o/r/issues?per_page=100") == [1, 2, 3, 4, 5]
    assert len(seen) == progress.MAX_PAGES == 5


def test_http_client_never_follows_a_foreign_next_link(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []
    evil = '<https://evil.example/x>; rel="next"'
    answers = [FakeResponse([1], link=evil), FakeResponse([2])]
    monkeypatch.setattr(progress, "urlopen", _urlopen_from(answers, seen))
    assert progress.HttpJson()("/repos/o/r/issues") == [1]
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("code", "headers"),
    [
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000"}),
        (429, {"X-RateLimit-Reset": "5000"}),
    ],
)
def test_http_client_reports_rate_limit_reset(
    monkeypatch: pytest.MonkeyPatch, code: int, headers: dict[str, str]
) -> None:
    msg = Message()
    for key, value in headers.items():
        msg[key] = value
    seen: list[urllib.request.Request] = []
    answers: list[Any] = [HTTPError("u", code, "limit", msg, None)]
    monkeypatch.setattr(progress, "urlopen", _urlopen_from(answers, seen))
    with pytest.raises(progress.RateLimited) as err:
        progress.HttpJson()("/repos/o/r/issues")
    assert err.value.until == 5000


def test_plain_403_is_not_a_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[Any] = [HTTPError("u", 403, "interdit", Message(), None)]
    monkeypatch.setattr(progress, "urlopen", _urlopen_from(answers, []))
    with pytest.raises(HTTPError):
        progress.HttpJson()("/repos/o/r/issues")


# Compléments (#26) : étapes, animation active seule, reduced-motion, cas limites
def _style() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    return "".join(re.findall(r"<style>(.*?)</style>", text, re.S))


def test_every_agent_step_is_reached_and_rendered(client: TestClient) -> None:
    # Scénario agents : étapes dev, revue, QA, PR, déploiement
    seed(
        (ago(minutes=3), "SubagentStart", "qa", "ag-12", None),
        (ago(minutes=2), "SubagentStart", "devops", "ag-13", None),
    )
    steps = {a["name"]: a["step"] for a in api(client)["agents"]}
    assert steps == {"qa": "qa", "devops": "deploy"}
    html = client.get("/progress").text
    assert "▶ QA" in html and "▶ Déploiement" in html
    # carte QA : étapes avant « QA » fermées (✓), étapes suivantes ouvertes (○)
    assert "✓ Dév" in html and "✓ Revue" in html and "○ PR" in html


def test_pulse_animation_only_on_active_agent_card(client: TestClient) -> None:
    # Scénario agents : animation discrète tant que l'agent est actif
    seed(
        (ago(minutes=2), "SubagentStart", "reviewer", "ag-10", None),
        (ago(minutes=30), "SubagentStop", "qa", "ag-11", None),
    )
    html = client.get("/progress").text
    assert html.count('class="pulse"') == 1
    reviewer_card = html.split('aria-label="reviewer"', 1)[1].split("</article>", 1)[0]
    assert 'class="pulse"' in reviewer_card


def test_reduced_motion_covers_every_animated_selector() -> None:
    # Scénario accessibilité : toute animation est coupée sous prefers-reduced-motion
    style = _style()
    head, _, media = style.partition("@media (prefers-reduced-motion: reduce)")
    reduced = re.search(r"\{\s*([^{}]*)\{([^{}]*)\}", media)
    assert reduced and "animation: none" in reduced.group(2)
    covered = {sel.strip().split()[-1] for sel in reduced.group(1).split(",")}
    rules = re.findall(r"([^{}@]+)\{([^{}]*)\}", head)
    animated = {
        sel.strip().split()[-1]
        for sel, body in rules
        if "animation:" in body and "animation: none" not in body
    }
    assert animated, "aucune animation détectée : le test ne vaut rien"
    assert animated <= covered, animated - covered


def test_timeout_is_absorbed_and_last_data_is_kept(client: TestClient, gh: FakeGithub) -> None:
    # Timeout réseau : pas de 500, dernières données conservées, bandeau d'erreur
    api(client)
    gh.error = TimeoutError("délai dépassé")
    gh.clock.now += 181  # type: ignore[attr-defined]
    body = api(client)
    assert body["github_error_at"] is not None
    assert body["totals"]["counts"]["delivered"] == 1
    assert client.get("/progress").status_code == 200


def test_timeout_is_passed_to_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_timeouts: list[float] = []

    def fake(req: urllib.request.Request, timeout: float) -> FakeResponse:
        seen_timeouts.append(timeout)
        return FakeResponse([])

    monkeypatch.setattr(progress, "urlopen", fake)
    progress.HttpJson()("/repos/o/r/issues")
    assert seen_timeouts == [progress.HTTP_TIMEOUT_S] and 0 < progress.HTTP_TIMEOUT_S <= 10


def test_arabic_darija_title_is_kept_intact_and_escaped(client: TestClient, gh: FakeGithub) -> None:
    title = 'شنو الجديد "<b>" ؟'
    gh.data["issues"] = [issue(2, labels=("epic",), epic=None, title=title)]
    assert api(client)["epics"][0]["title"] == title
    resp = client.get("/progress")
    assert "شنو الجديد" in resp.text
    assert "&lt;b&gt;" in resp.text and "<b>" not in resp.text


def test_empty_github_title_falls_back_to_number(client: TestClient, gh: FakeGithub) -> None:
    gh.data["issues"] = [issue(20, labels=("epic",), epic=None, title="")]
    assert api(client)["epics"][0]["title"] == "Ticket 20"


def test_every_status_segment_has_a_non_color_cue() -> None:
    style = _style()
    for key, _label in progress.STATUSES:
        rule = re.search(rf"\.seg-{key}\s*\{{([^}}]*)\}}", style)
        assert rule and re.search(r"gradient|box-shadow|border", rule.group(1)), key


def test_each_bar_has_a_visible_text_line_with_matching_swatches(client: TestClient) -> None:
    html = client.get("/progress").text
    row = html.split('<li class="row">', 1)[1].split("</li>", 1)[0]
    line = re.sub(r"<[^>]+>", "", row.split('<p class="counts">', 1)[1].split("</p>", 1)[0])
    assert " ".join(line.split()) == "Livré 1 · Revue 1 · En cours 1 · Prêt 1 · Backlog 2"
    for key, _label in progress.STATUSES:
        assert f'class="swatch seg-{key}"' in row, key
