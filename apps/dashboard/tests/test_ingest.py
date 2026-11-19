"""Ingestion authentifiée des événements d'agents (#3)."""

from __future__ import annotations

import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dashboard import db, ingest
from dashboard.main import app

TOKEN = "test-token-not-a-secret"  # noqa: S105 - jeton factice de test
URL = "/api/v1/events"
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "contracts" / "agent-event.schema.json"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv("INGEST_TOKEN", TOKEN)
    return path


@pytest.fixture
def client(db_path: Path) -> TestClient:
    return TestClient(app)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def event(event_id: str = "evt-1", **extra: Any) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "ts": "2026-11-18T10:00:00+00:00",
        "hook_event_name": "PreToolUse",
        "session_id": "sess-1",
        "agent_type": "backend",
        "agent_id": "agent-1",
        "tool_name": "Bash",
        **extra,
    }


def count_events(path: Path) -> int:
    if not path.exists():
        return 0
    with sqlite3.connect(path) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE name = 'events'").fetchall()
        if not tables:
            return 0
        row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
    return int(row[0])


# Scénario : événement valide accepté
def test_valid_event_is_accepted_and_stored(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=event(), headers=auth())
    assert resp.status_code == 202
    assert resp.json() == {"inserted": 1, "duplicates": 0}
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT event_id, hook_event_name, agent_id, tool_name FROM events"
        ).fetchone()
    assert row == ("evt-1", "PreToolUse", "agent-1", "Bash")


def test_optional_fields_may_be_null_or_absent(client: TestClient, db_path: Path) -> None:
    minimal = {k: v for k, v in event().items() if k not in {"agent_id", "tool_name"}}
    minimal["agent_type"] = None
    resp = client.post(URL, json=minimal, headers=auth())
    assert resp.status_code == 202
    assert count_events(db_path) == 1


def test_ts_is_stored_in_utc(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=event(ts="2026-11-18T11:00:00+01:00"), headers=auth())
    assert resp.status_code == 202
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT ts FROM events").fetchone()
    assert row == ("2026-11-18T10:00:00+00:00",)


@pytest.mark.parametrize("ts", [1763460000, "2026-11-18T10:00:00"], ids=["entier", "sans-fuseau"])
def test_ts_must_be_aware_datetime_string(client: TestClient, db_path: Path, ts: int | str) -> None:
    assert client.post(URL, json=event(ts=ts), headers=auth()).status_code == 422
    assert count_events(db_path) == 0


# Taille du corps
def test_body_over_limit_is_413(client: TestClient, db_path: Path) -> None:
    body = b" " * (ingest.MAX_BODY_BYTES + 1)
    resp = client.post(URL, content=body, headers=auth())
    assert resp.status_code == 413
    assert count_events(db_path) == 0


def test_body_over_limit_without_content_length_is_413(client: TestClient, db_path: Path) -> None:
    def chunks() -> Any:
        for _ in range(ingest.MAX_BODY_BYTES // 1024 + 2):
            yield b" " * 1024

    resp = client.post(URL, content=chunks(), headers=auth())
    assert resp.status_code == 413
    assert count_events(db_path) == 0


def test_body_size_checked_after_auth(client: TestClient) -> None:
    body = b" " * (ingest.MAX_BODY_BYTES + 1)
    assert client.post(URL, content=body, headers=auth("wrong")).status_code == 401


# Persistance
def test_schema_is_created_once_and_uses_wal(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = db.init_schema

    def spy(conn: sqlite3.Connection) -> None:
        calls.append("init")
        real(conn)

    monkeypatch.setattr(db, "init_schema", spy)
    client.post(URL, json=event("a"), headers=auth())
    client.post(URL, json=event("b"), headers=auth())
    assert len(calls) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_sqlite_runs_in_threadpool(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    offloaded: list[str] = []
    real = ingest.run_in_threadpool

    async def spy(func: Any, *args: Any) -> Any:
        offloaded.append(func.__name__)
        return await real(func, *args)

    monkeypatch.setattr(ingest, "run_in_threadpool", spy)
    assert client.post(URL, json=event(), headers=auth()).status_code == 202
    assert offloaded == ["store_events"]


# Scénario : jeton absent ou faux
@pytest.mark.parametrize(
    "headers",
    [{}, auth("wrong"), {"Authorization": TOKEN}, {"Authorization": "Basic abc"}],
    ids=["absent", "faux", "sans-bearer", "basic"],
)
def test_missing_or_wrong_token_is_401(
    client: TestClient, db_path: Path, headers: dict[str, str]
) -> None:
    resp = client.post(URL, json=event(), headers=headers)
    assert resp.status_code == 401
    assert count_events(db_path) == 0


def test_auth_is_checked_before_body_validation(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, content=b"{not json", headers=auth("wrong"))
    assert resp.status_code == 401
    resp = client.post(URL, json=event(prompt="secret"), headers={})
    assert resp.status_code == 401
    assert count_events(db_path) == 0


def test_token_compared_in_constant_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(ingest.hmac, "compare_digest", spy)
    client.post(URL, json=event(), headers=auth())
    assert calls


@pytest.mark.parametrize("value", [None, ""], ids=["absent", "vide"])
def test_missing_ingest_token_closes_service(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("INGEST_TOKEN")
    else:
        monkeypatch.setenv("INGEST_TOKEN", value)
    for headers in ({}, auth(""), auth(TOKEN)):
        resp = client.post(URL, json=event(), headers=headers)
        assert resp.status_code == 503
    assert count_events(db_path) == 0


# Scénario : idempotence
def test_resending_same_event_is_200_without_duplicate(client: TestClient, db_path: Path) -> None:
    assert client.post(URL, json=event(), headers=auth()).status_code == 202
    resp = client.post(URL, json=event(), headers=auth())
    assert resp.status_code == 200
    assert resp.json() == {"inserted": 0, "duplicates": 1}
    assert count_events(db_path) == 1


def test_batch_with_some_new_events_is_202(client: TestClient, db_path: Path) -> None:
    client.post(URL, json=event("a"), headers=auth())
    resp = client.post(URL, json=[event("a"), event("b")], headers=auth())
    assert resp.status_code == 202
    assert resp.json() == {"inserted": 1, "duplicates": 1}
    assert count_events(db_path) == 2


# Scénario : champ interdit
def test_unknown_field_is_422_and_nothing_written(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=event(prompt="raconte une histoire"), headers=auth())
    assert resp.status_code == 422
    assert count_events(db_path) == 0


@pytest.mark.parametrize("missing", ["event_id", "ts", "hook_event_name", "session_id"])
def test_missing_required_field_is_422(client: TestClient, db_path: Path, missing: str) -> None:
    body = event()
    del body[missing]
    assert client.post(URL, json=body, headers=auth()).status_code == 422
    assert count_events(db_path) == 0


def test_invalid_json_is_422(client: TestClient) -> None:
    resp = client.post(URL, content=b"{not json", headers=auth())
    assert resp.status_code == 422


# Lots
def test_batch_of_100_is_accepted(client: TestClient, db_path: Path) -> None:
    batch = [event(f"evt-{i}") for i in range(100)]
    assert client.post(URL, json=batch, headers=auth()).status_code == 202
    assert count_events(db_path) == 100


@pytest.mark.parametrize("size", [0, 101], ids=["vide", "trop-grand"])
def test_batch_out_of_bounds_is_422(client: TestClient, db_path: Path, size: int) -> None:
    batch = [event(f"evt-{i}") for i in range(size)]
    assert client.post(URL, json=batch, headers=auth()).status_code == 422
    assert count_events(db_path) == 0


def test_batch_is_atomic(client: TestClient, db_path: Path) -> None:
    batch = [event("ok-1"), event("ok-2"), event("bad", prompt="x")]
    assert client.post(URL, json=batch, headers=auth()).status_code == 422
    assert count_events(db_path) == 0


# Contrat : le modèle pydantic est aligné sur contracts/agent-event.schema.json
def test_model_matches_contract_schema() -> None:
    contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    model = ingest.AgentEvent.model_json_schema()
    assert contract["additionalProperties"] is False
    assert model["additionalProperties"] is False
    assert set(model["properties"]) == set(contract["properties"])
    assert set(model["required"]) == set(contract["required"])
    for name, spec in contract["properties"].items():
        nullable = isinstance(spec["type"], list) and "null" in spec["type"]
        types = {t.get("type") for t in model["properties"][name].get("anyOf", [])}
        assert nullable == ("null" in types), name


# Cas limites (QA)
def test_unicode_darija_roundtrip(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=event("evt-ع", tool_name="كتابة"), headers=auth())
    assert resp.status_code == 202
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT event_id, tool_name FROM events").fetchone()
    assert row == ("evt-ع", "كتابة")


def test_duplicate_inside_same_batch_stored_once(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=[event("d"), event("d")], headers=auth())
    assert resp.status_code == 202
    assert count_events(db_path) == 1


@pytest.mark.parametrize("bad", [{"event_id": ""}, {"ts": "2026-11-18T10:00:00"}, {"ts": "hier"}])
def test_empty_id_and_invalid_ts_are_422(client: TestClient, db_path: Path, bad: dict) -> None:
    assert client.post(URL, json=event(**bad), headers=auth()).status_code == 422
    assert count_events(db_path) == 0


def test_indexes_on_ts_and_agent_id(client: TestClient, db_path: Path) -> None:
    client.post(URL, json=event(), headers=auth())
    with sqlite3.connect(db_path) as conn:
        cols = {
            c[2]
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            for c in conn.execute(f'PRAGMA index_info("{name}")')
        }
    assert {"ts", "agent_id"} <= cols


# Ticket en cours (#100) : champ optionnel, entier >= 1, rétrocompatible
def stored_tickets(path: Path) -> list[tuple[str, int | None]]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT event_id, ticket FROM events ORDER BY event_id").fetchall()
    return [(str(event_id), ticket) for event_id, ticket in rows]


def test_ticket_is_stored(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=event(ticket=93), headers=auth())
    assert resp.status_code == 202
    assert stored_tickets(db_path) == [("evt-1", 93)]


# Scénario : ancien événement sans ticket stocké avec ticket NULL
@pytest.mark.parametrize("extra", [{}, {"ticket": None}])
def test_event_without_ticket_is_stored_null(
    client: TestClient, db_path: Path, extra: dict[str, Any]
) -> None:
    resp = client.post(URL, json=event(**extra), headers=auth())
    assert resp.status_code == 202
    assert stored_tickets(db_path) == [("evt-1", None)]


# Scénario : ticket négatif ou textuel refusé (contrat)
@pytest.mark.parametrize("bad", [-1, 0, "93", "#93", True, 9.5])
def test_invalid_ticket_is_422(client: TestClient, db_path: Path, bad: object) -> None:
    assert client.post(URL, json=event(ticket=bad), headers=auth()).status_code == 422
    assert count_events(db_path) == 0


LEGACY_EVENTS = """
CREATE TABLE events (
    event_id TEXT PRIMARY KEY, ts TEXT NOT NULL, hook_event_name TEXT NOT NULL,
    session_id TEXT NOT NULL, agent_type TEXT, agent_id TEXT, tool_name TEXT,
    received_at TEXT NOT NULL DEFAULT 'x'
);
INSERT INTO events (event_id, ts, hook_event_name, session_id)
VALUES ('old', '2026-11-01T10:00:00+00:00', 'PreToolUse', 'sess-0');
"""


def test_legacy_database_gains_ticket_column(client: TestClient, db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(LEGACY_EVENTS)
    resp = client.post(URL, json=event(ticket=7), headers=auth())
    assert resp.status_code == 202
    assert stored_tickets(db_path) == [("evt-1", 7), ("old", None)]


def test_ticket_migration_is_idempotent(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(LEGACY_EVENTS)
        db.init_schema(conn)
        db.init_schema(conn)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(events)")]
    assert cols.count("ticket") == 1


def test_contract_ticket_is_positive_integer() -> None:
    contract = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    spec = contract["properties"]["ticket"]
    assert spec["type"] == ["integer", "null"]
    assert spec["minimum"] == 1
    assert "ticket" not in contract["required"]
