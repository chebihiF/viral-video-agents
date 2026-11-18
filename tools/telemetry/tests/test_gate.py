"""Hook PreToolUse pretool_gate.py (#8) : code 2 si pause, échec sûr (cache 5 min)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from time import sleep, time
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "pretool_gate.py"
CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "control-api.md"
TOKEN = "jeton-de-test-pas-un-secret"  # noqa: S105 - jeton factice de test
SENSITIVE = "contenu-sensible-XYZ"
STDIN = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": SENSITIVE})
DEAD_URL = "http://127.0.0.1:9/api/v1/control"  # port « discard » : connexion refusée


@dataclass
class Stub:
    url: str
    seen: list[dict[str, str]] = field(default_factory=list)


@contextmanager
def server(status: int = 200, body: object = None, location: str | None = None) -> Iterator[Stub]:
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append({"path": self.path, "auth": self.headers.get("Authorization", "")})
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield Stub(f"http://127.0.0.1:{httpd.server_port}/api/v1/control", seen)
    finally:
        httpd.shutdown()
        httpd.server_close()


@contextmanager
def stalling_server() -> Iterator[str]:
    """Répond 200 puis distille 1 octet / 0,5 s : chaque lecture passe sous le délai de 2 s."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "4096")
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    sleep(0.5)
            except OSError:
                pass

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/api/v1/control"
    finally:
        httpd.shutdown()
        httpd.server_close()


def state(paused: bool, reason: str | None = None) -> dict[str, object]:
    return {
        "paused": paused,
        "reason": reason,
        "cap_tokens": 500_000,
        "tokens_24h": 1,
        "updated_at": "2026-11-18T12:00:00.000Z",
    }


def run_gate(
    tmp_path: Path, url: str | None, token: str | None = TOKEN, stdin: str = STDIN
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VVA_", "CLAUDE_"))}
    env["VVA_GATE_CACHE"] = str(tmp_path / "gate-cache.json")
    env["VVA_GATE_LOG"] = str(tmp_path / "gate.log")
    if url is not None:
        env["VVA_CONTROL_URL"] = url
    if token is not None:
        env["VVA_INGEST_TOKEN"] = token
    return subprocess.run(  # noqa: S603 - interpréteur courant, script du dépôt
        [sys.executable, str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )


def write_cache(tmp_path: Path, paused: bool, age_s: float, reason: str | None = None) -> None:
    cache = {"paused": paused, "reason": reason, "fetched_at": time() - age_s}
    (tmp_path / "gate-cache.json").write_text(json.dumps(cache))


def log_text(tmp_path: Path) -> str:
    path = tmp_path / "gate.log"
    return path.read_text() if path.exists() else ""


# --- Scénario : hook bloquant -----------------------------------------------------------------
def test_paused_exits_2_with_clear_message(tmp_path: Path) -> None:
    with server(body=state(True, "plafond")) as stub:
        proc = run_gate(tmp_path, stub.url)
    assert proc.returncode == 2
    assert "pause" in proc.stderr.lower()
    assert "plafond" in proc.stderr
    assert proc.stdout == ""


def test_manual_pause_message_gives_the_reason(tmp_path: Path) -> None:
    with server(body=state(True, "manuel")) as stub:
        proc = run_gate(tmp_path, stub.url)
    assert proc.returncode == 2
    assert "manuel" in proc.stderr


# --- Scénario : reprise -------------------------------------------------------------------------
def test_running_exits_0_silently(tmp_path: Path) -> None:
    with server(body=state(False)) as stub:
        proc = run_gate(tmp_path, stub.url)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_resume_overrides_a_paused_cache(tmp_path: Path) -> None:
    write_cache(tmp_path, paused=True, age_s=30)
    with server(body=state(False)) as stub:
        assert run_gate(tmp_path, stub.url).returncode == 0
    assert json.loads((tmp_path / "gate-cache.json").read_text())["paused"] is False


def test_bearer_header_is_sent_to_the_configured_url(tmp_path: Path) -> None:
    with server(body=state(False)) as stub:
        run_gate(tmp_path, stub.url)
    assert stub.seen == [{"path": "/api/v1/control", "auth": f"Bearer {TOKEN}"}]


# --- Scénario : échec sûr -----------------------------------------------------------------------
def test_unreachable_without_cache_lets_through_and_logs(tmp_path: Path) -> None:
    proc = run_gate(tmp_path, DEAD_URL)
    assert proc.returncode == 0
    assert "fail-open" in log_text(tmp_path)


def test_unreachable_uses_fresh_cache_paused(tmp_path: Path) -> None:
    write_cache(tmp_path, paused=True, age_s=240, reason="manuel")
    proc = run_gate(tmp_path, DEAD_URL)
    assert proc.returncode == 2
    assert "manuel" in proc.stderr
    assert "cache" in log_text(tmp_path)


def test_unreachable_uses_fresh_cache_running(tmp_path: Path) -> None:
    write_cache(tmp_path, paused=False, age_s=60)
    assert run_gate(tmp_path, DEAD_URL).returncode == 0


def test_unreachable_with_expired_cache_lets_through(tmp_path: Path) -> None:
    write_cache(tmp_path, paused=True, age_s=301)
    proc = run_gate(tmp_path, DEAD_URL)
    assert proc.returncode == 0
    assert "fail-open" in log_text(tmp_path)


def test_cache_from_the_future_is_not_trusted(tmp_path: Path) -> None:
    write_cache(tmp_path, paused=True, age_s=-3600)
    assert run_gate(tmp_path, DEAD_URL).returncode == 0


def test_global_deadline_uses_fresh_paused_cache(tmp_path: Path) -> None:
    write_cache(tmp_path, paused=True, age_s=60, reason="plafond")
    with stalling_server() as url:
        proc = run_gate(tmp_path, url)
    assert proc.returncode == 2
    assert "plafond" in proc.stderr
    assert "deadline" in log_text(tmp_path)


@pytest.mark.parametrize("cache", [None, "expired", "running"])
def test_global_deadline_otherwise_lets_through(tmp_path: Path, cache: str | None) -> None:
    if cache == "expired":
        write_cache(tmp_path, paused=True, age_s=301)
    elif cache == "running":
        write_cache(tmp_path, paused=False, age_s=60)
    with stalling_server() as url:
        proc = run_gate(tmp_path, url)
    assert proc.returncode == 0
    assert "fail-open deadline" in log_text(tmp_path)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, state(True, "manuel")),
        (500, b"boom"),
        (200, b"pas du json"),
        (200, {"paused": "oui"}),
        (200, [1, 2]),
        (401, {"detail": "jeton invalide"}),
    ],
)
def test_server_errors_and_bad_payloads_count_as_unreachable(
    tmp_path: Path, status: int, body: object
) -> None:
    with server(status=status, body=body) as stub:
        proc = run_gate(tmp_path, stub.url)
    assert proc.returncode == 0
    assert "fail-open" in log_text(tmp_path)


def test_redirect_is_not_followed(tmp_path: Path) -> None:
    with server(status=302, body={}, location="http://127.0.0.1:9/ailleurs") as stub:
        proc = run_gate(tmp_path, stub.url)
    assert proc.returncode == 0
    assert "fail-open" in log_text(tmp_path)


@pytest.mark.parametrize("url", [None, "ftp://exemple.org/x", "http://exemple.org/api/v1/control"])
def test_missing_or_unsafe_url_lets_through_and_logs(tmp_path: Path, url: str | None) -> None:
    proc = run_gate(tmp_path, url)
    assert proc.returncode == 0
    assert "config" in log_text(tmp_path)


def test_missing_token_lets_through_and_logs(tmp_path: Path) -> None:
    proc = run_gate(tmp_path, DEAD_URL, token=None)
    assert proc.returncode == 0
    assert "config" in log_text(tmp_path)


def test_garbage_stdin_never_breaks_the_hook(tmp_path: Path) -> None:
    with server(body=state(False)) as stub:
        assert run_gate(tmp_path, stub.url, stdin="\x00pas du json").returncode == 0


# --- Aucune donnée sensible ---------------------------------------------------------------------
def test_log_and_cache_hold_no_secret_nor_tool_data(tmp_path: Path) -> None:
    run_gate(tmp_path, DEAD_URL)
    with server(body=state(True, "plafond")) as stub:
        run_gate(tmp_path, stub.url)
    seen = log_text(tmp_path) + (tmp_path / "gate-cache.json").read_text()
    for forbidden in (TOKEN, SENSITIVE, "127.0.0.1", "Bash", "Bearer"):
        assert forbidden not in seen, forbidden


def test_state_files_are_private(tmp_path: Path) -> None:
    with server(body=state(False)) as stub:
        run_gate(tmp_path, stub.url)
    run_gate(tmp_path, DEAD_URL)
    for name in ("gate-cache.json", "gate.log"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600, name


# --- Contrat ------------------------------------------------------------------------------------
def test_contract_documents_exit_code_and_failsafe() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    for expected in ("code 2", "fail-open", "300", "VVA_GATE_CACHE", "VVA_GATE_LOG"):
        assert expected in text, expected
