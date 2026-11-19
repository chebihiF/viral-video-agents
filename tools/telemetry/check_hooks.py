#!/usr/bin/env python3
"""make telemetry-check : les hooks locaux émettent-ils vers l'ingestion ? (#170)

Lit `.claude/settings.json` en LECTURE SEULE et l'environnement ; les variables
VVA_INGEST_URL / VVA_INGEST_TOKEN sont seulement testées présentes (valeurs jamais
affichées). Aucun envoi réseau sans `--live`. Code 0 = tout est branché, 1 = à corriger.
Dépendance : jsonschema (groupe dev), donc à lancer via `uv run` (voir le Makefile).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]  # pas de stubs, pas de dépendance ajoutée

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "contracts" / "agent-event.schema.json"
MODEL = "docs/examples/hooks-settings.example.json"
LEGACY = "log_event.sh"
ENV_VARS = ("VVA_INGEST_URL", "VVA_INGEST_TOKEN")
# événement -> script attendu (échec si absent) ; la porte de pause n'est qu'un avertissement
REQUIRED = {
    "PostToolUse": "hook_emit.py",
    "PostToolUseFailure": "hook_emit.py",
    "SubagentStop": "hook_emit.py",
    "Stop": "ship.py",
}
OPTIONAL = {"PreToolUse": "pretool_gate.py"}


def load_settings(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("la racine doit être un objet JSON")
    return data


def commands_by_event(settings: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    hooks = settings.get("hooks")
    for event, groups in (hooks if isinstance(hooks, dict) else {}).items():
        found = result.setdefault(event, [])
        for group in groups if isinstance(groups, list) else []:
            for item in group.get("hooks", []) if isinstance(group, dict) else []:
                command = item.get("command") if isinstance(item, dict) else None
                if isinstance(command, str):
                    found.append(command)
    return result


def hook_problems(commands: dict[str, list[str]]) -> list[str]:
    problems: list[str] = []
    legacy = sorted(e for e, cmds in commands.items() if any(LEGACY in c for c in cmds))
    if legacy:
        problems.append(
            f"l'ancien {LEGACY} est encore appelé ({', '.join(legacy)}) : il n'envoie rien au "
            f"dashboard. Remplacez ces hooks par ceux de {MODEL}."
        )
    for event, script in REQUIRED.items():
        if not any(script in c for c in commands.get(event, [])):
            problems.append(
                f"hook {event} : {script} absent des réglages. Copiez-le depuis {MODEL}."
            )
    return problems


def hook_warnings(commands: dict[str, list[str]]) -> list[str]:
    return [
        f"hook {event} : {script} absent (pause des agents inactive). Voir {MODEL}."
        for event, script in OPTIONAL.items()
        if not any(script in c for c in commands.get(event, []))
    ]


def env_problems(settings: dict[str, Any]) -> list[str]:
    block = settings.get("env")
    declared = block if isinstance(block, dict) else {}
    return [
        f'variable {name} absente (environnement ou bloc "env" de .claude/settings.json).'
        for name in ENV_VARS
        if not (os.environ.get(name) or declared.get(name))
    ]


def synthetic_event() -> dict[str, Any] | None:
    """Événement construit par le vrai émetteur (hook_emit.build_event), sans écriture."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hook_emit

    event = hook_emit.build_event(
        {"hook_event_name": "PostToolUse", "session_id": "telemetry-check", "tool_name": "Bash"}
    )
    return dict(event) if event else None


def synthetic_event_errors() -> list[str]:
    event = synthetic_event()
    if event is None:
        return ["hook_emit.py n'a produit aucun événement pour un hook valide."]
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    return [f"événement factice invalide : {e.message}" for e in validator.iter_errors(event)]


def live_problems(settings: dict[str, Any]) -> list[str]:
    """--live : envoie l'événement factice avec ship.py (seul cas d'accès réseau)."""
    event = synthetic_event()
    if event is None:
        return ["envoi réel impossible : pas d'événement factice."]
    block = settings.get("env")
    declared = block if isinstance(block, dict) else {}
    env = {**os.environ, **{k: str(v) for k, v in declared.items()}}
    with tempfile.TemporaryDirectory() as tmp:
        queue = Path(tmp) / "events.jsonl"
        queue.write_text(json.dumps(event) + "\n", encoding="utf-8")
        env["VVA_TELEMETRY_FILE"] = str(queue)
        proc = subprocess.run(  # noqa: S603 - interpréteur courant, script du dépôt
            [sys.executable, str(Path(__file__).resolve().parent / "ship.py")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
    if proc.returncode != 0:
        return [f"envoi réel refusé (ship.py code {proc.returncode}) : vérifiez URL et jeton."]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=ROOT / ".claude" / "settings.json")
    parser.add_argument("--live", action="store_true", help="envoie vraiment un événement factice")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.settings)
    except (OSError, ValueError) as exc:
        message = f"telemetry-check : réglages illisibles ({args.settings.name}) : {exc}"
        print(message, file=sys.stderr)
        return 1
    commands = commands_by_event(settings)
    problems = hook_problems(commands) + env_problems(settings) + synthetic_event_errors()
    if not problems and args.live:
        problems = live_problems(settings)
    for warning in hook_warnings(commands):
        print(f"telemetry-check : avertissement : {warning}")
    if problems:
        for problem in problems:
            print(f"telemetry-check : {problem}", file=sys.stderr)
        print(f"telemetry-check : ÉCHEC ({len(problems)} problème(s)).", file=sys.stderr)
        return 1
    print("telemetry-check : ok, les hooks émettent vers l'ingestion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
