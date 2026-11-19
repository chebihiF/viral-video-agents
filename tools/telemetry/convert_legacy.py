#!/usr/bin/env python3
"""Convertisseur ponctuel : ancien events.jsonl -> agent-event, sans doublon (#166).

Ancien format (`log_event.sh`) : hook_event_name, session_id, agent_type, agent_id,
tool_name et `ts` en secondes epoch (flottant), sans `event_id`. Sortie conforme à
contracts/agent-event.schema.json ; envoi par lots via ship.post (contracts/ingest-api.md).

Usage : convert_legacy.py <fichier> [--dry-run | --send]   (dry-run par défaut)
`--send` lit VVA_INGEST_URL et VVA_INGEST_TOKEN dans l'environnement ; le jeton n'est
jamais affiché. Liste blanche de champs : aucun prompt, sortie d'outil, chemin ni nom
de branche ne sort. `event_id` = sha256 de la ligne source normalisée : rejouable sans
doublon (le serveur déduplique par event_id). Lignes inconvertibles : comptées, ignorées.
Stdlib uniquement ; compatible Python 3.9 (python3 du Mac).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ship
from hook_emit import BRANCH

REQUIRED = {"hook_event_name": 64, "session_id": 128}
OPTIONAL = {"agent_type": 128, "agent_id": 128, "tool_name": 128}
BRANCH_KEYS = ("branch", "git_branch")
TICKET_MAX = 2_147_483_647


@dataclass
class Report:
    events: list[dict[str, Any]] = field(default_factory=list)
    ignored: int = 0

    @property
    def with_ticket(self) -> int:
        return sum(1 for e in self.events if "ticket" in e)


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _iso(ts: Any) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()  # noqa: UP017 - Python 3.9
    except (OverflowError, OSError, ValueError):
        return None


def _ticket(line: dict[str, Any]) -> int | None:
    for key in BRANCH_KEYS:
        value = line.get(key)
        match = BRANCH.match(value) if isinstance(value, str) else None
        if match and 1 <= int(match.group(1)) <= TICKET_MAX:
            return int(match.group(1))
    return None


def event_id(line: dict[str, Any]) -> str:
    """sha256 de la ligne source normalisée (clés triées, séparateurs compacts)."""
    normalized = json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def convert(line: Any) -> dict[str, Any] | None:
    """Événement agent-event d'une ligne ancien format, ou None si inconvertible."""
    if not isinstance(line, dict):
        return None
    ts = _iso(line.get("ts"))
    required = {name: _text(line.get(name), limit) for name, limit in REQUIRED.items()}
    if ts is None or any(v is None for v in required.values()):
        return None
    event: dict[str, Any] = {"event_id": event_id(line), "ts": ts, **required}
    for name, limit in OPTIONAL.items():
        value = _text(line.get(name), limit)
        if value is not None:
            event[name] = value
    ticket = _ticket(line)
    if ticket is not None:
        event["ticket"] = ticket
    return event


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def convert_file(path: Path) -> Report:
    report = Report()
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            event = convert(_parse(raw))
            if event is None:
                report.ignored += 1
            else:
                report.events.append(event)
    return report


def print_report(report: Report, mode: str) -> None:
    print(f"convert_legacy ({mode})")
    print(f"converties : {len(report.events)}")
    print(f"ignorées : {report.ignored}")
    print(f"ticket déduit : {report.with_ticket}")


def _send_each(cfg: ship.Config, batch: list[dict[str, Any]]) -> ship.Outcome:
    rejected = 0
    for event in batch:
        outcome = ship.post(cfg, [event])
        if outcome is ship.Outcome.REJECTED:
            rejected += 1
        elif outcome is not ship.Outcome.OK:
            return outcome
    if rejected:
        print(f"convert_legacy: {rejected} événement(s) refusé(s) par le serveur", file=sys.stderr)
    return ship.Outcome.OK


def send(cfg: ship.Config, events: list[dict[str, Any]]) -> ship.Outcome:
    """Envoi par lots de 100 au plus ; s'arrête au premier échec (rejouable sans doublon)."""
    for start in range(0, len(events), ship.BATCH_MAX):
        batch = events[start : start + ship.BATCH_MAX]
        outcome = ship.post(cfg, batch)
        if outcome is ship.Outcome.REJECTED:
            outcome = _send_each(cfg, batch)
        if outcome is not ship.Outcome.OK:
            return outcome
    return ship.Outcome.OK


def _config(path: Path) -> ship.Config | None:
    url = os.environ.get("VVA_INGEST_URL", "")
    token = os.environ.get("VVA_INGEST_TOKEN", "")
    if not url or not token:
        print("convert_legacy: VVA_INGEST_URL et VVA_INGEST_TOKEN sont requis", file=sys.stderr)
        return None
    if not ship.url_allowed(url):
        print("convert_legacy: VVA_INGEST_URL en https requis (http : localhost)", file=sys.stderr)
        return None
    return ship.Config(events_file=path, url=url, token=token)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    do_send = "--send" in args
    if do_send and "--dry-run" in args:
        print("convert_legacy: --send et --dry-run sont exclusifs", file=sys.stderr)
        return ship.EXIT_CONFIG
    paths = [a for a in args if not a.startswith("--")]
    if len(paths) != 1:
        print("usage : convert_legacy.py <fichier> [--dry-run | --send]", file=sys.stderr)
        return ship.EXIT_CONFIG
    path = Path(paths[0])
    cfg = _config(path) if do_send else None
    if do_send and cfg is None:
        return ship.EXIT_CONFIG
    if not path.is_file():
        print(f"convert_legacy: fichier introuvable : {path.name}", file=sys.stderr)
        return ship.EXIT_CONFIG
    report = convert_file(path)
    print_report(report, "send" if do_send else "dry-run")
    if cfg is None:
        return ship.EXIT_OK
    outcome = send(cfg, report.events)
    if outcome is ship.Outcome.CONFIG:
        print("convert_legacy: réponse 3xx/4xx (URL ou jeton à corriger)", file=sys.stderr)
        return ship.EXIT_CONFIG
    if outcome is ship.Outcome.RETRY:
        print("convert_legacy: serveur injoignable ou en erreur, relancer", file=sys.stderr)
        return ship.EXIT_RETRY
    return ship.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
