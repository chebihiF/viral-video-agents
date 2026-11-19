"""Émetteur de hooks : stdin JSON -> ligne JSONL minimale (#4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from dashboard import ingest

SCRIPT = Path(__file__).resolve().parents[1] / "hook_emit.py"
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "contracts" / "agent-event.schema.json"
SENSITIVE = "contenu-sensible-XYZ"


def run_hook(
    payload: str, tmp_path: Path, target: Path | None = None
) -> tuple[subprocess.CompletedProcess[str], float]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VVA_")}
    env.pop("CLAUDE_PROJECT_DIR", None)
    if target is not None:
        env["VVA_TELEMETRY_FILE"] = str(target)
    start = time.monotonic()
    proc = subprocess.run(  # noqa: S603 - interpréteur courant, script du dépôt
        [sys.executable, str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=5,
        check=False,
    )
    return proc, time.monotonic() - start


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def subagent_start() -> dict[str, Any]:
    return {
        "hook_event_name": "SubagentStart",
        "session_id": "sess-1",
        "agent_type": "dev-backend",
        "agent_id": "agent-42",
        "transcript_path": "/Users/x/.claude/projects/t.jsonl",
        "cwd": "/Users/x/dev",
    }


def post_tool_use() -> dict[str, Any]:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-1",
        "agent_id": "agent-42",
        "agent_type": "dev-backend",
        "tool_name": "Bash",
        "prompt": SENSITIVE,
        "tool_input": {"command": f"echo {SENSITIVE}"},
        "tool_response": {"stdout": SENSITIVE},
        "tool_use_id": "toolu_1",
    }


def post_tool_use_failure() -> dict[str, Any]:
    return {
        "hook_event_name": "PostToolUseFailure",
        "session_id": "sess-1",
        "agent_id": "agent-42",
        "agent_type": "dev-backend",
        "tool_name": "Bash",
        "error": f"Command failed: {SENSITIVE}",
        "is_interrupt": False,
        "tool_input": {"command": f"echo {SENSITIVE}"},
        "tool_use_id": "toolu_2",
    }


# Scénario : hook écrit un événement
def test_subagent_start_appends_line(tmp_path: Path) -> None:
    target = tmp_path / "t" / "events.jsonl"
    proc, _ = run_hook(json.dumps(subagent_start()), tmp_path, target)
    assert proc.returncode == 0
    [line] = lines(target)
    assert line["hook_event_name"] == "SubagentStart"
    assert line["session_id"] == "sess-1"
    assert line["agent_type"] == "dev-backend"
    assert line["agent_id"] == "agent-42"
    assert line["tool_name"] is None
    assert line["ts"].endswith("+00:00")
    uuid.UUID(line["event_id"])


def test_default_path_is_claude_telemetry(tmp_path: Path) -> None:
    proc, _ = run_hook(json.dumps(subagent_start()), tmp_path)
    assert proc.returncode == 0
    assert len(lines(tmp_path / ".claude" / "telemetry" / "events.jsonl")) == 1


def test_lines_are_appended_with_unique_ids(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    for _ in range(3):
        run_hook(json.dumps(subagent_start()), tmp_path, target)
    ids = [line["event_id"] for line in lines(target)]
    assert len(set(ids)) == 3


# Scénario : aucun contenu sensible
def test_post_tool_use_has_no_sensitive_content(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    run_hook(json.dumps(post_tool_use()), tmp_path, target)
    raw = target.read_text(encoding="utf-8")
    assert SENSITIVE not in raw
    [line] = lines(target)
    assert line["tool_name"] == "Bash"
    assert set(line) == {
        "event_id",
        "ts",
        "hook_event_name",
        "session_id",
        "agent_type",
        "agent_id",
        "tool_name",
    }


# Scénario : échec d'outil (#116), statut « Erreur » du dashboard
def test_post_tool_use_failure_is_emitted_without_error_text(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    proc, _ = run_hook(json.dumps(post_tool_use_failure()), tmp_path, target)
    assert proc.returncode == 0
    assert SENSITIVE not in target.read_text(encoding="utf-8")
    [line] = lines(target)
    assert line["hook_event_name"] == "PostToolUseFailure"
    assert (line["agent_type"], line["agent_id"], line["tool_name"]) == (
        "dev-backend",
        "agent-42",
        "Bash",
    )
    assert set(line) == {
        "event_id",
        "ts",
        "hook_event_name",
        "session_id",
        "agent_type",
        "agent_id",
        "tool_name",
    }


# Contrat : la ligne est conforme à contracts/agent-event.schema.json
@pytest.mark.parametrize("payload", [subagent_start(), post_tool_use(), post_tool_use_failure()])
def test_line_matches_contract(tmp_path: Path, payload: dict[str, Any]) -> None:
    target = tmp_path / "events.jsonl"
    run_hook(json.dumps(payload), tmp_path, target)
    [line] = lines(target)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert set(line) <= set(schema["properties"])
    assert set(schema["required"]) <= set(line)
    ingest.AgentEvent.model_validate_json(json.dumps(line))


def test_overlong_values_are_truncated_to_contract(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    payload = subagent_start() | {"agent_type": "a" * 500, "session_id": "s" * 500}
    run_hook(json.dumps(payload), tmp_path, target)
    [line] = lines(target)
    ingest.AgentEvent.model_validate_json(json.dumps(line))


def test_non_string_values_become_null(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    run_hook(json.dumps(subagent_start() | {"agent_id": {"x": SENSITIVE}}), tmp_path, target)
    [line] = lines(target)
    assert line["agent_id"] is None


@pytest.mark.parametrize(
    "payload",
    ["", "pas du json", "[1, 2]", json.dumps({"hook_event_name": "X"})],
)
def test_invalid_input_writes_nothing_and_exits_0(tmp_path: Path, payload: str) -> None:
    target = tmp_path / "events.jsonl"
    proc, _ = run_hook(payload, tmp_path, target)
    assert proc.returncode == 0
    assert not target.exists()


# Scénario : le hook ne bloque jamais
def test_write_failure_exits_0_fast(tmp_path: Path) -> None:
    blocker = tmp_path / "fichier"
    blocker.write_text("x", encoding="utf-8")
    proc, elapsed = run_hook(json.dumps(subagent_start()), tmp_path, blocker / "events.jsonl")
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert elapsed < 1.0


def test_stdin_never_closed_exits_0_under_one_second(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VVA_")}
    env["VVA_TELEMETRY_FILE"] = str(tmp_path / "events.jsonl")
    start = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - interpréteur courant, script du dépôt
        [sys.executable, str(SCRIPT)], stdin=subprocess.PIPE, env=env, cwd=tmp_path
    )
    try:
        code = proc.wait(timeout=3)
    finally:
        proc.kill()
        assert proc.stdin is not None
        proc.stdin.close()
    assert code == 0
    assert time.monotonic() - start < 1.5


def test_darija_arabic_values_roundtrip_utf8(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    payload = subagent_start() | {"agent_type": "وكيل-مطوّر", "session_id": "جلسة-1"}
    proc, _ = run_hook(json.dumps(payload, ensure_ascii=False), tmp_path, target)
    assert proc.returncode == 0
    [line] = lines(target)
    assert line["agent_type"] == "وكيل-مطوّر"
    assert line["session_id"] == "جلسة-1"
    ingest.AgentEvent.model_validate_json(json.dumps(line))


def test_concurrent_hooks_write_whole_lines(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith("VVA_")}
    env["VVA_TELEMETRY_FILE"] = str(target)
    procs = [
        subprocess.Popen(  # noqa: S603 - interpréteur courant, script du dépôt
            [sys.executable, str(SCRIPT)], stdin=subprocess.PIPE, text=True, env=env
        )
        for _ in range(8)
    ]
    for proc in procs:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(subagent_start()))
        proc.stdin.close()
    assert [p.wait(timeout=5) for p in procs] == [0] * 8
    assert len({line["event_id"] for line in lines(target)}) == 8


# Revue : stdin lu en entier (pas de troncature du JSON au-delà de 1 Mio)
def test_large_payload_is_read_entirely(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    payload = post_tool_use() | {"tool_response": {"stdout": "x" * (3 << 20)}}
    proc, _ = run_hook(json.dumps(payload), tmp_path, target)
    assert proc.returncode == 0
    [line] = lines(target)
    assert line["tool_name"] == "Bash"


# Ticket en cours (#100) : déduit de la branche du `cwd` du hook, uniquement le nombre
def git_repo(path: Path, branch: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["symbolic-ref", "HEAD", f"refs/heads/{branch}"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)  # noqa: S603, S607
    return path


def emitted_in(tmp_path: Path, branch: str) -> dict[str, Any]:
    repo = git_repo(tmp_path / "repo", branch)
    target = tmp_path / "events.jsonl"
    proc, _ = run_hook(json.dumps(subagent_start() | {"cwd": str(repo)}), tmp_path, target)
    assert proc.returncode == 0
    [line] = lines(target)
    return line


# Scénario : branche feat/93-accueil -> ticket 93
@pytest.mark.parametrize(
    ("branch", "ticket"),
    [
        ("feat/93-accueil", 93),
        ("fix/7-bug", 7),
        ("chore/120-ci", 120),
        ("ci/999999999-x", 999_999_999),
    ],
)
def test_ticket_comes_from_branch(tmp_path: Path, branch: str, ticket: int) -> None:
    line = emitted_in(tmp_path, branch)
    assert line["ticket"] == ticket
    ingest.AgentEvent.model_validate_json(json.dumps(line))


# Scénario : branche sans numéro -> ticket absent, ingestion 2xx
@pytest.mark.parametrize(
    "branch", ["main", "feat/accueil", "feat/93", "wip/93-x", "feat/1234567890-trop-long"]
)
def test_branch_without_number_has_no_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415 - seul test HTTP du fichier

    from dashboard.main import app  # noqa: PLC0415

    line = emitted_in(tmp_path, branch)
    assert "ticket" not in line
    monkeypatch.setenv("VVA_DB_PATH", str(tmp_path / "vva.db"))
    monkeypatch.setenv("INGEST_TOKEN", "test-token-not-a-secret")
    headers = {"Authorization": "Bearer test-token-not-a-secret"}
    resp = TestClient(app).post("/api/v1/events", json=line, headers=headers)
    assert resp.status_code == 202


def test_missing_or_bad_cwd_has_no_ticket(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    for cwd in (None, 42, str(tmp_path / "absent")):
        run_hook(json.dumps(subagent_start() | {"cwd": cwd}), tmp_path, target)
    assert all("ticket" not in line for line in lines(target))
    assert len(lines(target)) == 3
