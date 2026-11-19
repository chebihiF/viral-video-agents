#!/usr/bin/env python3
"""Hook Claude Code : JSON du hook sur stdin -> une ligne JSONL minimale (#4).

Contrat de sortie : contracts/agent-event.schema.json. Seules des métadonnées sont
écrites (jamais prompt, arguments ni sortie d'outil). Le hook ne bloque jamais :
toute erreur, ou plus de ~0,8 s d'exécution, se termine en code 0 sans sortie.
Stdlib uniquement ; compatible Python 3.9 (python3 du Mac, #59).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any

DEFAULT_FILE = Path(".claude") / "telemetry" / "events.jsonl"
DEADLINE_S = 0.8
REQUIRED = {"hook_event_name": 64, "session_id": 128}
OPTIONAL = {"agent_type": 128, "agent_id": 128, "tool_name": 128}
# même motif que tools/agents/token_report.py : seul le numéro sort de la branche (#100)
BRANCH = re.compile(r"^(?:feat|fix|chore|docs|test|ci)/(\d+)-")
GIT_TIMEOUT_S = 0.4


def _bail(_signum: int, _frame: FrameType | None) -> None:
    os._exit(0)


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def ticket_of(cwd: Any) -> int | None:
    """Numéro de la branche courante de `cwd` ; None hors branche de ticket ou si git échoue."""
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - git local, liste d'arguments sans shell
            ["git", "-C", cwd, "branch", "--show-current"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    match = BRANCH.match(out.stdout.strip())
    return int(match.group(1)) if match else None


def build_event(hook: Any) -> dict[str, str | int | None] | None:
    """Projette le JSON du hook sur le contrat ; None si inexploitable."""
    if not isinstance(hook, dict):
        return None
    event: dict[str, str | int | None] = {
        "event_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 - pas de datetime.UTC en 3.9
    }
    for name, limit in REQUIRED.items():
        value = _text(hook.get(name), limit)
        if value is None:
            return None
        event[name] = value
    for name, limit in OPTIONAL.items():
        event[name] = _text(hook.get(name), limit)
    ticket = ticket_of(hook.get("cwd"))
    if ticket is not None:
        event["ticket"] = ticket
    return event


def target_file() -> Path:
    configured = os.environ.get("VVA_TELEMETRY_FILE")
    if configured:
        return Path(configured)
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")) / DEFAULT_FILE


def append_line(path: Path, event: dict[str, str | int | None]) -> None:
    """Ajout en un seul write O_APPEND : lignes entières même si hooks concurrents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def main() -> int:
    try:
        signal.signal(signal.SIGALRM, _bail)
        signal.setitimer(signal.ITIMER_REAL, DEADLINE_S)
        event = build_event(json.loads(sys.stdin.read()))
        if event is not None:
            append_line(target_file(), event)
    except BaseException:  # noqa: BLE001, S110 - le hook ne doit jamais échouer
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
