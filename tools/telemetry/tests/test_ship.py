"""Expéditeur : JSONL -> POST /api/v1/events par lots, sans perte ni doublon (#4).

Serveur HTTP local factice uniquement : jamais le VPS.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

import ship

TOKEN = "jeton-factice-de-test"  # noqa: S105 - jeton factice de test

Responder = Callable[[list[dict[str, Any]]], int]


class FakeIngest:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.auth: list[str | None] = []
        self.respond: Responder = lambda _batch: 202
        self.location: str | None = None
        self.hits = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - API http.server
                fake.hits += 1
                self.send_response(200)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802 - API http.server
                fake.hits += 1
                body = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(body)
                batch = payload if isinstance(payload, list) else [payload]
                fake.auth.append(self.headers.get("Authorization"))
                code = fake.respond(batch)
                if code in (200, 202):
                    fake.batches.append(batch)
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                if fake.location:
                    self.send_header("Location", fake.location)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args: Any) -> None:
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v1/events"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def received_ids(self) -> list[str]:
        return [e["event_id"] for batch in self.batches for e in batch]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server() -> Iterator[FakeIngest]:
    fake = FakeIngest()
    yield fake
    fake.close()


@pytest.fixture
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


def event(i: int) -> dict[str, Any]:
    return {
        "event_id": f"evt-{i}",
        "ts": "2026-11-18T10:00:00+00:00",
        "hook_event_name": "PostToolUse",
        "session_id": "sess-1",
        "agent_type": None,
        "agent_id": None,
        "tool_name": "Bash",
    }


def write_events(path: Path, start: int, count: int) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for i in range(start, start + count):
            fh.write(json.dumps(event(i)) + "\n")


def config(path: Path, url: str) -> ship.Config:
    return ship.Config(events_file=path, url=url, token=TOKEN, timeout=2.0)


def closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/api/v1/events"


def test_sends_all_events_with_bearer_token(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 3)
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    assert server.received_ids() == ["evt-0", "evt-1", "evt-2"]
    assert set(server.auth) == {f"Bearer {TOKEN}"}


def test_batches_are_at_most_100(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 250)
    ship.run(config(events_file, server.url))
    assert [len(b) for b in server.batches] == [100, 100, 50]


def test_rerun_sends_nothing_twice(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 2)
    ship.run(config(events_file, server.url))
    write_events(events_file, 2, 1)
    ship.run(config(events_file, server.url))
    ship.run(config(events_file, server.url))
    assert server.received_ids() == ["evt-0", "evt-1", "evt-2"]


def test_200_all_duplicates_counts_as_success(server: FakeIngest, events_file: Path) -> None:
    server.respond = lambda _batch: 200
    write_events(events_file, 0, 2)
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    server.respond = lambda _batch: 202
    ship.run(config(events_file, server.url))
    assert len(server.batches) == 1


def test_partial_trailing_line_is_not_sent(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 1)
    with events_file.open("a", encoding="utf-8") as fh:
        fh.write('{"event_id": "evt-1", "ts"')
    ship.run(config(events_file, server.url))
    assert server.received_ids() == ["evt-0"]
    with events_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event(1))[len('{"event_id": "evt-1", "ts"') :] + "\n")
    ship.run(config(events_file, server.url))
    assert server.received_ids() == ["evt-0", "evt-1"]


# Scénario : VPS injoignable
def test_unreachable_keeps_queue_then_resends_without_loss_or_dup(
    server: FakeIngest, events_file: Path
) -> None:
    write_events(events_file, 0, 120)
    assert ship.run(config(events_file, closed_port_url())) == ship.EXIT_RETRY
    write_events(events_file, 120, 5)
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    ids = server.received_ids()
    assert ids == [f"evt-{i}" for i in range(125)]


def test_5xx_keeps_queue_and_retries(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 3)
    server.respond = lambda _batch: 503
    assert ship.run(config(events_file, server.url)) == ship.EXIT_RETRY
    server.respond = lambda _batch: 202
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    assert server.received_ids() == ["evt-0", "evt-1", "evt-2"]


def test_5xx_on_second_batch_keeps_first_acknowledged(
    server: FakeIngest, events_file: Path
) -> None:
    write_events(events_file, 0, 150)
    calls: list[int] = []

    def flaky(_batch: list[dict[str, Any]]) -> int:
        calls.append(1)
        return 202 if len(calls) == 1 else 500

    server.respond = flaky
    assert ship.run(config(events_file, server.url)) == ship.EXIT_RETRY
    server.respond = lambda _batch: 202
    ship.run(config(events_file, server.url))
    assert server.received_ids() == [f"evt-{i}" for i in range(150)]


def test_4xx_isolates_bad_event_without_blocking(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 5)
    server.respond = lambda batch: 422 if any(e["event_id"] == "evt-2" for e in batch) else 202
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    assert sorted(server.received_ids()) == ["evt-0", "evt-1", "evt-3", "evt-4"]
    rejected = ship.rejected_path(events_file).read_text(encoding="utf-8").splitlines()
    assert [json.loads(r)["event_id"] for r in rejected] == ["evt-2"]
    write_events(events_file, 5, 1)
    ship.run(config(events_file, server.url))
    assert "evt-5" in server.received_ids()
    assert server.received_ids().count("evt-0") == 1


def test_invalid_json_line_is_isolated(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 1)
    with events_file.open("a", encoding="utf-8") as fh:
        fh.write("pas du json\n")
    write_events(events_file, 1, 1)
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    assert server.received_ids() == ["evt-0", "evt-1"]
    assert ship.rejected_path(events_file).read_text(encoding="utf-8") == "pas du json\n"


def test_401_keeps_queue_and_signals_config(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 2)
    server.respond = lambda _batch: 401
    assert ship.run(config(events_file, server.url)) == ship.EXIT_CONFIG
    assert not ship.rejected_path(events_file).exists()
    server.respond = lambda _batch: 202
    ship.run(config(events_file, server.url))
    assert server.received_ids() == ["evt-0", "evt-1"]


def test_truncated_file_restarts_from_zero(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 3)
    ship.run(config(events_file, server.url))
    events_file.write_text(json.dumps(event(9)) + "\n", encoding="utf-8")
    ship.run(config(events_file, server.url))
    assert server.received_ids()[-1] == "evt-9"


def test_missing_file_is_ok(server: FakeIngest, events_file: Path) -> None:
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    assert server.batches == []


@pytest.mark.parametrize("missing", ["VVA_INGEST_URL", "VVA_INGEST_TOKEN"])
def test_main_requires_env(
    monkeypatch: pytest.MonkeyPatch, events_file: Path, missing: str
) -> None:
    monkeypatch.setenv("VVA_TELEMETRY_FILE", str(events_file))
    monkeypatch.setenv("VVA_INGEST_URL", "http://127.0.0.1:9/api/v1/events")
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    monkeypatch.delenv(missing)
    assert ship.main() == ship.EXIT_CONFIG


def test_main_reads_env(
    monkeypatch: pytest.MonkeyPatch, server: FakeIngest, events_file: Path
) -> None:
    write_events(events_file, 0, 1)
    monkeypatch.setenv("VVA_TELEMETRY_FILE", str(events_file))
    monkeypatch.setenv("VVA_INGEST_URL", server.url)
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    assert ship.main() == ship.EXIT_OK
    assert server.received_ids() == ["evt-0"]


def test_rejects_plain_http_on_remote_host(events_file: Path) -> None:
    write_events(events_file, 0, 1)
    assert ship.run(config(events_file, "http://example.com/api/v1/events")) == ship.EXIT_CONFIG


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://dash.example.com/api/v1/events", True),
        ("http://localhost:8000/api/v1/events", True),
        ("http://127.0.0.1:8000/api/v1/events", True),
        ("http://[::1]:8000/api/v1/events", True),
        ("http://dash.example.com/api/v1/events", False),
        ("http://127.0.0.1.evil.com/api/v1/events", False),
        ("ftp://localhost/x", False),
    ],
)
def test_url_policy(url: str, allowed: bool) -> None:
    assert ship.url_allowed(url) is allowed


# Revue : aucune redirection suivie (le jeton ne doit jamais partir ailleurs)
@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_redirect_is_not_followed_and_keeps_queue(
    server: FakeIngest, events_file: Path, code: int
) -> None:
    elsewhere = FakeIngest()
    try:
        write_events(events_file, 0, 2)
        server.respond = lambda _batch: code
        server.location = elsewhere.url
        assert ship.run(config(events_file, server.url)) == ship.EXIT_CONFIG
        assert elsewhere.hits == 0
        assert elsewhere.auth == []
        assert not ship.rejected_path(events_file).exists()
    finally:
        elsewhere.close()
    server.respond = lambda _batch: 202
    server.location = None
    ship.run(config(events_file, server.url))
    assert server.received_ids() == ["evt-0", "evt-1"]


# Revue : seuls 413 et 422 isolent un événement ; tout autre 4xx garde la file
@pytest.mark.parametrize("code", [400, 404, 405, 409])
def test_other_4xx_is_config_and_keeps_queue(
    server: FakeIngest, events_file: Path, code: int
) -> None:
    write_events(events_file, 0, 2)
    server.respond = lambda _batch: code
    assert ship.run(config(events_file, server.url)) == ship.EXIT_CONFIG
    assert not ship.rejected_path(events_file).exists()
    server.respond = lambda _batch: 202
    ship.run(config(events_file, server.url))
    assert server.received_ids() == ["evt-0", "evt-1"]


def test_413_isolates_oversized_event(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 3)
    server.respond = lambda batch: 413 if any(e["event_id"] == "evt-1" for e in batch) else 202
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    assert server.received_ids() == ["evt-0", "evt-2"]
    assert ship.rejected_path(events_file).exists()


# Revue : fichiers d'état privés
def test_state_files_are_private(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 2)
    server.respond = lambda batch: 422 if any(e["event_id"] == "evt-1" for e in batch) else 202
    ship.run(config(events_file, server.url))
    for path in (
        ship.cursor_path(events_file),
        ship.rejected_path(events_file),
        ship.lock_path(events_file),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


# Revue : fichier remplacé (inode différent) -> reprise depuis 0
def test_replaced_file_restarts_from_zero(
    server: FakeIngest, events_file: Path, tmp_path: Path
) -> None:
    write_events(events_file, 0, 3)
    ship.run(config(events_file, server.url))
    fresh = tmp_path / "fresh.jsonl"
    write_events(fresh, 10, 5)
    os.replace(fresh, events_file)
    ship.run(config(events_file, server.url))
    assert server.received_ids()[3:] == [f"evt-{i}" for i in range(10, 15)]


def test_rejects_non_http_url(events_file: Path) -> None:
    write_events(events_file, 0, 1)
    assert ship.run(config(events_file, "file:///etc/passwd")) == ship.EXIT_CONFIG


def test_empty_file_is_ok_and_sends_nothing(server: FakeIngest, events_file: Path) -> None:
    events_file.write_text("", encoding="utf-8")
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    assert server.batches == []


def test_crash_before_cursor_save_resends_same_ids(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 2)
    ship.run(config(events_file, server.url))
    ship.cursor_path(events_file).unlink()  # panne entre l'envoi et l'écriture du curseur
    ship.run(config(events_file, server.url))
    assert server.received_ids() == ["evt-0", "evt-1", "evt-0", "evt-1"]  # le serveur déduplique


def test_second_shipper_does_not_run_concurrently(server: FakeIngest, events_file: Path) -> None:
    write_events(events_file, 0, 1)
    with ship.exclusive(events_file) as first:
        assert first
        assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    assert server.batches == []
    ship.run(config(events_file, server.url))
    assert server.received_ids() == ["evt-0"]


def test_unicode_darija_event_is_sent_intact(server: FakeIngest, events_file: Path) -> None:
    e = event(0) | {"agent_type": "وكيل-مطوّر"}
    events_file.write_text(json.dumps(e, ensure_ascii=False) + "\n", encoding="utf-8")
    assert ship.run(config(events_file, server.url)) == ship.EXIT_OK
    assert server.batches[0][0]["agent_type"] == "وكيل-مطوّر"
