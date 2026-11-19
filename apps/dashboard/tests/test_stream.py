"""Flux SSE /stream (#6) : session exigée, id croissant, reprise par Last-Event-ID."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, db, stream
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
STATIC = Path(stream.__file__).parent / "static"


@pytest.fixture
def db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    monkeypatch.setattr(stream, "POLL_S", 0.01)
    monkeypatch.setattr(stream, "STREAM_MAX_S", 0.1)  # la réponse HTTP se termine seule
    auth.LOGIN_LIMITER.reset()
    return path


@pytest.fixture
def client(db_file: Path) -> TestClient:
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


def seed(n: int, start: int = 0, **extra: Any) -> None:
    rows = [
        (
            f"evt-{i:03d}",
            f"2026-11-18T10:00:{i % 60:02d}+00:00",
            "PreToolUse",
            "sess-1",
            extra.get("agent_type", "backend"),
            extra.get("agent_id", "agent-1"),
            extra.get("tool_name", "Bash"),
        )
        for i in range(start, start + n)
    ]
    with db.connect() as conn:
        db.insert_events(conn, rows)


def parse(body: str) -> list[tuple[int, dict[str, Any]]]:
    """(id, données JSON) de chaque message SSE portant un événement."""
    out = []
    for block in body.split("\n\n"):
        m = re.fullmatch(r"id: (\d+)\ndata: (.+)", block)
        if m:
            out.append((int(m.group(1)), json.loads(m.group(2))))
    return out


def read_stream(client: TestClient, **headers: str) -> str:
    with client.stream("GET", "/stream", headers=headers) as resp:
        assert resp.status_code == 200
        return "".join(resp.iter_text())


# Scénario : flux authentifié
def test_stream_requires_session(db_file: Path) -> None:
    anon = TestClient(app, base_url="https://testserver", follow_redirects=False)
    resp = anon.get("/stream")
    assert resp.status_code == 401
    assert "text/event-stream" not in resp.headers.get("content-type", "")


def test_stream_rejects_forged_cookie(db_file: Path) -> None:
    anon = TestClient(app, base_url="https://testserver", follow_redirects=False)
    anon.cookies.set(auth.COOKIE_NAME, "9999999999.forge")
    assert anon.get("/stream").status_code == 401


def test_anonymous_stream_never_reads_events(
    db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(stream, "events_after", lambda *a, **k: calls.append(1) or [])
    monkeypatch.setattr(stream, "last_event_id", lambda: calls.append(1) or 0)
    anon = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert anon.get("/stream").status_code == 401
    assert calls == []


def test_stream_headers(client: TestClient) -> None:
    with client.stream("GET", "/stream") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert "no-store" in resp.headers["cache-control"]
        assert resp.headers["x-accel-buffering"] == "no"


# Format
def test_message_has_increasing_id_and_json_payload(client: TestClient) -> None:
    seed(3)
    events = parse(read_stream(client, **{"Last-Event-ID": "0"}))
    ids = [i for i, _ in events]
    assert len(ids) == 3 and ids == sorted(ids) and len(set(ids)) == 3
    first = events[0][1]
    assert first == {
        "event_id": "evt-000",
        "ts": "2026-11-18T10:00:00+00:00",
        "time_label": "2026-11-18 10:00:00",
        "hook_event_name": "PreToolUse",
        "agent_type": "backend",
        "agent_id": "agent-1",
        "tool_name": "Bash",
    }


def test_payload_cannot_inject_extra_sse_fields(client: TestClient) -> None:
    seed(1, tool_name="x\n\nid: 999\ndata: pwn")
    body = read_stream(client, **{"Last-Event-ID": "0"})
    assert len(parse(body)) == 1
    assert "\nid: 999" not in body


# Scénario : reconnexion
def test_resume_with_last_event_id_has_no_loss_and_no_duplicate(client: TestClient) -> None:
    seed(5)
    first = parse(read_stream(client, **{"Last-Event-ID": "0"}))
    cut = first[1][0]  # la connexion retombe après le 2e message
    seed(2, start=5)  # arrivent pendant la coupure
    resumed = parse(read_stream(client, **{"Last-Event-ID": str(cut)}))
    seen = [d["event_id"] for _, d in first[:2]] + [d["event_id"] for _, d in resumed]
    assert seen == [f"evt-{i:03d}" for i in range(7)]


def test_without_cursor_starts_after_latest_event(client: TestClient) -> None:
    seed(3)
    assert parse(read_stream(client)) == []


def test_after_query_parameter_is_the_initial_cursor(client: TestClient) -> None:
    seed(3)
    with client.stream("GET", "/stream", params={"after": "1"}) as resp:
        events = parse("".join(resp.iter_text()))
    assert [d["event_id"] for _, d in events] == ["evt-001", "evt-002"]


def test_header_wins_over_query_and_garbage_is_ignored(client: TestClient) -> None:
    seed(3)
    with client.stream(
        "GET", "/stream", params={"after": "0"}, headers={"Last-Event-ID": "2"}
    ) as r:
        assert [d["event_id"] for _, d in parse("".join(r.iter_text()))] == ["evt-002"]
    for bad in ("abc", "-1", "1" * 40, ""):
        assert parse(read_stream(client, **{"Last-Event-ID": bad})) == []


def test_stream_unavailable_when_database_unreadable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(stream, "last_event_id", broken)
    resp = client.get("/stream")
    assert resp.status_code == 503
    assert "disk I/O" not in resp.text


# Scénario : mise à jour en direct (générateur finissable, sans blocage)
def collect(**kwargs: Any) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in stream.event_stream(**kwargs)]

    return asyncio.run(run())


def test_generator_emits_retry_then_events_and_ends(db_file: Path) -> None:
    seed(2)
    chunks = collect(after=0, poll_s=0.0, max_polls=1, keepalive_every=100)
    assert chunks[0] == f"retry: {stream.RETRY_MS}\n\n"
    assert len(chunks) == 3 and all(c.startswith("id: ") for c in chunks[1:])


def test_generator_sends_keepalive_comments_when_idle(db_file: Path) -> None:
    chunks = collect(after=0, poll_s=0.0, max_polls=3, keepalive_every=1)
    assert chunks[1:] == [": keepalive\n\n"] * 3


def test_generator_ends_quietly_when_database_fails(
    db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(after: int, limit: int) -> list[Any]:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(stream, "events_after", broken)
    assert len(collect(after=0, poll_s=0.0, max_polls=5, keepalive_every=1)) == 1


def test_new_event_reaches_open_stream_in_under_2_seconds(db_file: Path) -> None:
    async def run() -> float:
        gen = stream.event_stream(after=0, poll_s=stream.POLL_S, max_polls=1000, keepalive_every=50)
        assert (await anext(gen)).startswith("retry:")
        started = time.monotonic()
        seed(1)  # événement ingéré pendant que le flux est ouvert
        chunk = await anext(gen)
        elapsed = time.monotonic() - started
        await gen.aclose()
        assert chunk.startswith("id: 1\n")
        return elapsed

    assert asyncio.run(run()) < 2


def test_poll_interval_is_well_under_2_seconds() -> None:
    assert stream.POLL_S <= 1.0


# Client JS : servi localement, sans CDN
def test_live_script_served_locally_without_remote_resources(client: TestClient) -> None:
    resp = client.get("/static/live.js")
    assert resp.status_code == 200
    assert "EventSource" in resp.text
    assert not re.search(r"https?://", resp.text)
    assert "innerHTML" not in resp.text  # DOM construit via textContent, jamais de HTML injecté


def test_contract_documents_stream() -> None:
    text = (Path(stream.__file__).parents[4] / "contracts" / "dashboard-routes.md").read_text()
    for needle in ("/stream", "text/event-stream", "Last-Event-ID", "retry", "keepalive"):
        assert needle in text, needle


def test_activity_page_is_wired_to_the_stream(client: TestClient) -> None:
    seed(3)
    html = client.get("/activity").text
    pattern = r'<main id="contenu" data-stream data-last-id="3" data-refresh="#agents">'
    assert re.search(pattern, html)
    assert '<ol aria-live="polite" data-feed>' in html
    assert '<script src="/static/live.js" defer></script>' in html


def test_activity_error_page_has_no_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(conn: sqlite3.Connection, limit: int = 100) -> list[Any]:
        raise sqlite3.OperationalError("x")

    monkeypatch.setattr("dashboard.activity.latest_events", broken)
    assert "data-stream" not in client.get("/activity").text
