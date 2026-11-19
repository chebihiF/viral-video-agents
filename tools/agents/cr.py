#!/usr/bin/env python3
"""CR d'agents en base SQLite : hook `record` et lecture `status` (#61). Voir docs/agents.md.

`record` (hooks SubagentStop et SessionEnd, JSON sur stdin) extrait le CR du dernier message
(rubriques Fait / Fichiers / Reste / Blocage, 5 lignes au plus, secrets masqués) et l'ajoute à
la table `agent_reports` (contrat : contracts/agent-report.schema.json). Il ne bloque jamais :
toute erreur, base illisible comprise, se termine en code 0 sans sortie. `status` affiche en
10 lignes au plus les CR arrivés depuis la dernière lecture. Stdlib ; Python 3.9.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any

DEFAULT_DB = Path.home() / ".vva" / "agent-reports.db"
DEADLINE_S = 1.5
BUSY_S = 0.5
MAX_LINES = 5
MAX_LINE_CHARS = 190
MAX_STATUS_LINES = 10
TAIL_BYTES = 1_000_000
UTC = timezone.utc  # noqa: UP017 - pas de datetime.UTC en 3.9
LABELS = {"fait": "Fait", "fichier": "Fichiers", "reste": "Reste", "blocage": "Blocage"}
RUBRIC = re.compile(
    r"^[\s>*-]*(fait|fichiers?|reste(?:\s+à\s+faire)?|blocages?)\**\s*[:–-]\s*\**\s*(.*?)\**\s*$",
    re.IGNORECASE,
)
EMPTY = re.compile(
    r"^(?:(?:rien|aucune?|néant|ras|none|n/a|[-–—])"
    r"|(?:aucune?|pas\s+de|plus\s+de|sans|rien)\s+"
    r"(?:à\s+(?:faire|signaler)|blocages?|restes?(?:\s+à\s+faire)?|problèmes?))?\.?$",
    re.IGNORECASE,
)
NO_REPORT = "Fait : (CR absent du dernier message)"
MASKED = "[masqué]"
DDL = (
    "CREATE TABLE IF NOT EXISTS agent_reports ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER, agent TEXT NOT NULL,"
    " ts TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def db_path() -> Path:
    return Path(os.environ.get("VVA_CR_DB") or DEFAULT_DB)


# --- extraction du CR -----------------------------------------------------------------------


def _label(word: str) -> str:
    return LABELS[word.lower().split()[0].rstrip("s")]


def parse_report(message: str) -> tuple[str, str]:
    """(résumé de 5 lignes au plus, statut). Sans rubrique : `no-cr`, rien n'est recopié."""
    found: list[tuple[str, str]] = []
    for raw in message.splitlines():
        match = RUBRIC.match(raw)
        if match:
            found.append((_label(match.group(1)), match.group(2)))
    if not found:
        return NO_REPORT, "no-cr"
    starts = [i for i, (label, _) in enumerate(found) if label == "Fait"]
    # Le dernier bloc (depuis son « Fait ») rend compte de la fin de tâche ; repli : 5 dernières.
    kept = found[starts[-1] :][:MAX_LINES] if starts else found[-MAX_LINES:]
    return "\n".join(_render(label, value) for label, value in kept), _status(dict(kept))


def _render(label: str, value: str) -> str:
    from handoff import SECRETS  # noqa: PLC0415 - import tardif : un échec reste dans le hook

    text = MASKED if SECRETS.search(value) else value
    return f"{label} : {text}"[:MAX_LINE_CHARS]


def _status(first: dict[str, str]) -> str:
    if not EMPTY.match(first.get("Blocage", "")):
        return "blocked"
    if not EMPTY.match(first.get("Reste", "")):
        return "partial"
    return "done"


def _text_of(entry: Any, main_only: bool) -> str:
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return ""
    if main_only and entry.get("isSidechain") is True:
        return ""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(t for t in texts if isinstance(t, str))


def last_assistant_text(transcript: Path, main_only: bool) -> str:
    """Dernier message texte de l'assistant dans la fin du transcript.

    `main_only` écarte les entrées de sous-agents (transcript de session) ; le transcript
    d'un sous-agent, lui, est entièrement « sidechain ».
    """
    with transcript.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - TAIL_BYTES))
        lines = handle.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            text = _text_of(json.loads(line), main_only)
        except ValueError:
            continue
        if text.strip():
            return text
    return ""


def _message(hook: dict[str, Any]) -> str:
    direct = hook.get("last_assistant_message")
    if isinstance(direct, str):
        return direct
    for key, main_only in (("agent_transcript_path", False), ("transcript_path", True)):
        path = hook.get(key)
        if isinstance(path, str) and path:
            return last_assistant_text(Path(path), main_only)
    return ""


def _agent(hook: dict[str, Any]) -> str:
    from token_report import NAME  # noqa: PLC0415 - import tardif (voir _render)

    name = hook.get("agent_type") if hook.get("hook_event_name") == "SubagentStop" else None
    name = name or "tech-lead"
    return name if isinstance(name, str) and NAME.match(name) else "?"


def _ticket(hook: dict[str, Any]) -> int | None:
    from token_report import _branch, ticket_of_branch  # noqa: PLC0415 - import tardif

    cwd = hook.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        return ticket_of_branch(_branch(cwd))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# --- base -------------------------------------------------------------------------------------


def connect(path: Path) -> sqlite3.Connection:
    """Ouvre la base en 0600 (création sous umask 077, base existante resserrée)."""
    old_umask = os.umask(0o077)
    try:
        conn = sqlite3.connect(path, timeout=BUSY_S)
        for statement in DDL:
            conn.execute(statement)
    finally:
        os.umask(old_umask)
    path.chmod(0o600)
    return conn


def record(hook_text: str, path: Path) -> None:
    hook = json.loads(hook_text)
    if not isinstance(hook, dict) or hook.get("hook_event_name") not in (
        "SubagentStop",
        "SessionEnd",
    ):
        return
    summary, status = parse_report(_message(hook))
    row = (_ticket(hook), _agent(hook), now(), summary, status)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = connect(path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO agent_reports (ticket, agent, ts, summary, status)"
                " VALUES (?, ?, ?, ?, ?)",
                row,
            )
    finally:
        conn.close()


def _bail(_signum: int, _frame: FrameType | None) -> None:
    os._exit(0)


def run_hook(path: Path) -> int:
    try:
        signal.signal(signal.SIGALRM, _bail)
        signal.setitimer(signal.ITIMER_REAL, DEADLINE_S)
        record(sys.stdin.read(), path)
    except BaseException:  # noqa: BLE001, S110 - le hook ne doit jamais interrompre l'agent
        pass
    return 0


# --- status -----------------------------------------------------------------------------------

NOTHING_NEW = "Aucun nouveau CR depuis la dernière lecture."


def _line(row: tuple[Any, ...]) -> str:
    ticket, agent, ts, summary, status = row
    where = f"#{ticket}" if ticket else "#-"
    when = f"{ts[:10]} {ts[11:16]}"
    return f"{where} {agent} {when} {status} | {summary.replace(chr(10), ' | ')}"[:200]


def status_lines(path: Path) -> list[str]:
    """CR arrivés depuis la dernière lecture (≤ 10 lignes) ; marque tout comme lu."""
    if not path.exists():
        return [NOTHING_NEW]
    conn = connect(path)
    try:
        with conn:
            seen = conn.execute("SELECT value FROM meta WHERE key = 'last_read'").fetchone()
            after = int(seen[0]) if seen else 0
            top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM agent_reports").fetchone()[0]
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_reports WHERE id > ?", (after,)
            ).fetchone()[0]
            shown = conn.execute(
                "SELECT ticket, agent, ts, summary, status FROM agent_reports WHERE id > ?"
                " ORDER BY id DESC LIMIT ?",
                (after, MAX_STATUS_LINES - 1),
            ).fetchall()
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_read', ?)", (str(top),)
            )
    finally:
        conn.close()
    if not count:
        return [NOTHING_NEW]
    plural = "nouveau CR" if count == 1 else "nouveaux CR"
    extra = f" ({len(shown)} derniers affichés)" if count > len(shown) else ""
    return [f"{count} {plural} depuis la dernière lecture{extra}."] + [
        _line(r) for r in reversed(shown)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CR d'agents en base SQLite (#61)")
    parser.add_argument("command", choices=["record", "status"])
    args = parser.parse_args(argv)
    if args.command == "record":
        return run_hook(db_path())
    try:
        print("\n".join(status_lines(db_path())))
    except (sqlite3.Error, OSError, ValueError):
        print("Base des CR illisible (VVA_CR_DB).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
