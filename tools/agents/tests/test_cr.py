"""CR d'agents en base SQLite et `make status` (#61). Faux secrets construits à l'exécution."""

from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import cr
import jsonschema  # type: ignore[import-untyped]  # pas de stubs (dépendance de dev)
import pytest

AGENTS = Path(__file__).resolve().parents[1]
REPO = AGENTS.parents[1]
SCRIPT = AGENTS / "cr.py"
SCHEMA = json.loads((REPO / "contracts" / "agent-report.schema.json").read_text())
CR_DONE = (
    "Fait : hook SubagentStop codé.\nFichiers : tools/agents/cr.py.\n"
    "Reste : rien.\nBlocage : aucun."
)
CR_PARTIAL = CR_DONE.replace("Reste : rien.", "Reste : câbler le hook.")
CR_BLOCKED = CR_DONE.replace("Blocage : aucun.", "Blocage : droit gh manquant.")


def run(
    args: list[str],
    db: Path,
    stdin: str = "",
    interpreter: str = sys.executable,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VVA_")}
    env["VVA_CR_DB"] = str(db)
    return subprocess.run(  # noqa: S603 - interpréteur local, script du dépôt
        [interpreter, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )


def repo_on_branch(tmp_path: Path, branch: str) -> Path:
    work = tmp_path / "poste"
    work.mkdir()
    subprocess.run(  # noqa: S603
        ["git", "init", "-q", "-b", branch, str(work)],  # noqa: S607
        check=True,
    )
    return work


def hook(message: str | None, cwd: Path, event: str = "SubagentStop", **extra: Any) -> str:
    payload: dict[str, Any] = {
        "hook_event_name": event,
        "session_id": "sess-1",
        "cwd": str(cwd),
        "agent_type": "dev-backend",
        **extra,
    }
    if message is not None:
        payload["last_assistant_message"] = message
    return json.dumps(payload)


def rows(db: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        found = conn.execute(
            "SELECT ticket, agent, ts, summary, status FROM agent_reports ORDER BY id"
        ).fetchall()
    return [dict(r) for r in found]


# --- Scénario : un agent termine une tâche ------------------------------------------------


def test_subagent_stop_adds_one_row(tmp_path: Path) -> None:
    db = tmp_path / "cr.db"
    work = repo_on_branch(tmp_path, "feat/61-cr-agents-sqlite")
    proc = run(["record"], db, hook(CR_PARTIAL, work))
    assert proc.returncode == 0
    assert (proc.stdout, proc.stderr) == ("", "")
    (row,) = rows(db)
    assert row["ticket"] == 61
    assert row["agent"] == "dev-backend"
    assert row["status"] == "partial"
    assert row["summary"].splitlines() == CR_PARTIAL.splitlines()
    jsonschema.validate(row, SCHEMA)


@pytest.mark.parametrize(
    ("message", "status"),
    [(CR_DONE, "done"), (CR_PARTIAL, "partial"), (CR_BLOCKED, "blocked")],
)
def test_status_from_reste_and_blocage(message: str, status: str) -> None:
    assert cr.parse_report(message)[1] == status


def test_summary_keeps_five_lines_and_masks_secrets() -> None:
    fake = "gh" + "p_" + "a1B2" * 9
    message = (
        "Intro libre\n"
        + CR_DONE.replace("aucun.", f"token: {fake}")
        + "\nReste : r2\nFichiers : f2\nReste : r3"
    )
    summary, status = cr.parse_report(message)
    assert len(summary.splitlines()) == 5
    assert fake not in summary
    assert "[masqué]" in summary
    assert status == "blocked"


@pytest.mark.parametrize(
    ("blocage", "reste", "status"),
    [
        ("aucun blocage", "rien à faire", "done"),
        ("pas de blocage.", "aucun reste", "done"),
        ("rien à signaler", "aucun reste à faire", "done"),
        ("sans blocage", "plus de reste", "done"),
        ("aucun, sauf le droit gh", "rien", "blocked"),
        ("aucun", "rien à faire sauf câbler", "partial"),
    ],
)
def test_empty_rubrics_with_a_prefix_word(blocage: str, reste: str, status: str) -> None:
    message = f"Fait : ok\nReste : {reste}\nBlocage : {blocage}"
    assert cr.parse_report(message)[1] == status


def test_last_cr_block_is_kept_not_the_first() -> None:
    earlier = CR_BLOCKED.replace("Fait :", "Fait : ancien")
    summary, status = cr.parse_report(earlier + "\nOn reprend.\n" + CR_DONE)
    assert summary.splitlines() == CR_DONE.splitlines()
    assert status == "done"


def test_short_final_block_does_not_inherit_the_old_blocage() -> None:
    final = "Fait : corrigé.\nFichiers : cr.py.\nReste : rien."
    summary, status = cr.parse_report("Fait : x\nBlocage : X\n\n" + final)
    assert summary.splitlines() == final.splitlines()
    assert status == "done"


def test_without_fait_the_last_five_rubrics_are_kept() -> None:
    message = "\n".join(f"Reste : r{i}" for i in range(7))
    assert cr.parse_report(message)[0].splitlines()[-1] == "Reste : r6"
    assert len(cr.parse_report(message)[0].splitlines()) == 5


def test_markdown_decorations_are_accepted() -> None:
    summary, status = cr.parse_report("- **Fait** : ok\n* Reste: rien\n> Blocage - aucun")
    assert status == "done"
    assert summary.splitlines() == ["Fait : ok", "Reste : rien", "Blocage : aucun"]


@pytest.mark.parametrize("message", ["", "Terminé, tout va bien.", "Faits divers : x"])
def test_message_without_cr_is_flagged_and_not_copied(message: str) -> None:
    summary, status = cr.parse_report(message)
    assert status == "no-cr"
    assert "Terminé" not in summary
    assert summary


def test_ticket_is_null_outside_a_ticket_branch(tmp_path: Path) -> None:
    db = tmp_path / "cr.db"
    work = repo_on_branch(tmp_path, "main")
    run(["record"], db, hook(CR_DONE, work))
    (row,) = rows(db)
    assert row["ticket"] is None
    jsonschema.validate(row, SCHEMA)


def test_session_end_reads_the_transcript_as_tech_lead(tmp_path: Path) -> None:
    db = tmp_path / "cr.db"
    work = repo_on_branch(tmp_path, "fix/7-x")
    transcript = tmp_path / "t.jsonl"
    entries = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ancien"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": CR_DONE}]}},
        {"type": "user", "message": {"content": "merci"}},
        {"type": "assistant", "isSidechain": True, "message": {"content": "sous-agent"}},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    stdin = hook(None, work, event="SessionEnd", transcript_path=str(transcript))
    stdin = json.dumps({k: v for k, v in json.loads(stdin).items() if k != "agent_type"})
    assert run(["record"], db, stdin).returncode == 0
    (row,) = rows(db)
    assert (row["agent"], row["ticket"], row["status"]) == ("tech-lead", 7, "done")


def test_subagent_stop_reads_its_own_transcript_with_sidechain_entries(tmp_path: Path) -> None:
    db = tmp_path / "cr.db"
    transcript = tmp_path / "agent.jsonl"
    entry = {
        "type": "assistant",
        "isSidechain": True,
        "message": {"content": [{"type": "text", "text": CR_BLOCKED}]},
    }
    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    stdin = hook(None, tmp_path, agent_transcript_path=str(transcript))
    assert run(["record"], db, stdin).returncode == 0
    (row,) = rows(db)
    assert (row["agent"], row["status"]) == ("dev-backend", "blocked")


def test_session_end_ignores_sidechain_entries_of_the_main_transcript(tmp_path: Path) -> None:
    db = tmp_path / "cr.db"
    transcript = tmp_path / "main.jsonl"
    entry = {
        "type": "assistant",
        "isSidechain": True,
        "message": {"content": [{"type": "text", "text": CR_DONE}]},
    }
    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    stdin = hook(None, tmp_path, event="SessionEnd", transcript_path=str(transcript))
    run(["record"], db, stdin)
    assert rows(db)[0]["status"] == "no-cr"


def test_invalid_agent_name_is_replaced(tmp_path: Path) -> None:
    db = tmp_path / "cr.db"
    stdin = hook(CR_DONE, tmp_path, agent_type="a b;rm")
    run(["record"], db, stdin)
    (row,) = rows(db)
    assert row["agent"] == "?"


# --- Scénario : le coordinateur reprend à froid -----------------------------------------


def seed(db: Path, count: int, tmp_path: Path) -> None:
    work = repo_on_branch(tmp_path, "feat/61-x") if not (tmp_path / "poste").exists() else None
    cwd = work or tmp_path / "poste"
    for i in range(count):
        run(["record"], db, hook(CR_DONE.replace("codé", f"codé {i}"), cwd))


def test_status_is_ten_lines_max_and_incremental(tmp_path: Path) -> None:
    db = tmp_path / "cr.db"
    seed(db, 12, tmp_path)
    first = run(["status"], db)
    assert first.returncode == 0
    out = first.stdout.splitlines()
    assert 2 <= len(out) <= 10
    assert "12" in out[0]
    assert "#61" in out[1]
    assert "dev-backend" in out[1]
    assert "codé 11" in out[-1]  # le plus récent en dernier
    again = run(["status"], db)
    assert again.stdout.splitlines() == ["Aucun nouveau CR depuis la dernière lecture."]
    seed(db, 1, tmp_path)
    third = run(["status"], db).stdout.splitlines()
    assert "1 nouveau" in third[0]
    assert len(third) == 2


def test_status_without_database_says_nothing_new(tmp_path: Path) -> None:
    proc = run(["status"], tmp_path / "absent.db")
    assert proc.returncode == 0
    assert proc.stdout.splitlines() == ["Aucun nouveau CR depuis la dernière lecture."]


# --- Scénario : base indisponible ---------------------------------------------------------


def test_unreadable_database_never_fails_the_hook(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"ceci n'est pas une base SQLite" * 50)
    directory = tmp_path / "dir.db"
    directory.mkdir()
    for db in (garbage, directory, tmp_path / "absent" / "x" / "cr.db"):
        proc = run(["record"], db, hook(CR_DONE, tmp_path))
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_missing_sibling_modules_never_fail_the_hook(tmp_path: Path) -> None:
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    shutil.copy(SCRIPT, lonely / "cr.py")  # sans handoff.py ni token_report.py
    env = {**os.environ, "VVA_CR_DB": str(tmp_path / "cr.db")}
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(lonely / "cr.py"), "record"],
        input=hook(CR_DONE, tmp_path),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_database_and_directory_are_private(tmp_path: Path) -> None:
    fresh = tmp_path / "nouveau" / "cr.db"
    run(["record"], fresh, hook(CR_DONE, tmp_path))
    assert fresh.stat().st_mode & 0o777 == 0o600
    assert fresh.parent.stat().st_mode & 0o777 == 0o700
    old = tmp_path / "ancienne.db"
    run(["record"], old, hook(CR_DONE, tmp_path))
    old.chmod(0o644)
    run(["record"], old, hook(CR_DONE, tmp_path))
    assert old.stat().st_mode & 0o777 == 0o600
    old.chmod(0o666)
    run(["status"], old)
    assert old.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("stdin", ["", "pas du json", "[]", '{"hook_event_name": "SubagentStop"}'])
def test_garbage_stdin_never_fails_the_hook(tmp_path: Path, stdin: str) -> None:
    proc = run(["record"], tmp_path / "cr.db", stdin)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_status_on_unreadable_database_is_one_line(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"ceci n'est pas une base SQLite" * 50)
    proc = run(["status"], garbage)
    assert proc.returncode == 1
    assert len(proc.stderr.splitlines()) == 1
    assert "Traceback" not in proc.stderr


# --- Contrat et Python 3.9 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"ticket": 0},
        {"agent": "a b"},
        {"status": "ok"},
        {"ts": "hier"},
        {"summary": ""},
        {"extra": 1},
    ],
)
def test_schema_rejects_invalid_rows(bad: dict[str, Any]) -> None:
    good = {"ticket": 1, "agent": "qa", "ts": cr.now(), "summary": "Fait : x", "status": "done"}
    jsonschema.validate(good, SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**good, **bad}, SCHEMA)


def test_syntax_is_python_39() -> None:
    ast.parse(SCRIPT.read_text(encoding="utf-8"), filename="cr.py", feature_version=(3, 9))


def test_hook_runs_under_real_python_39(tmp_path: Path) -> None:
    for name in ("python3.9", "/usr/bin/python3"):
        path = shutil.which(name)
        if path is None:
            continue
        probe = subprocess.run(  # noqa: S603
            [path, "-c", "import sys; print(sys.version_info < (3, 10))"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.stdout.strip() == "True":
            db = tmp_path / "cr.db"
            assert run(["record"], db, hook(CR_DONE, tmp_path), path).returncode == 0
            assert rows(db)[0]["status"] == "done"
            assert run(["status"], db, interpreter=path).returncode == 0
            return
    pytest.skip("aucun Python < 3.10 sur cette machine (couvert par le test AST)")
