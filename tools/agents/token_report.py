#!/usr/bin/env python3
"""Tokens par ticket, agent et modèle ; routage des modèles (#19). Voir docs/agents.md.

Lit la base SQLite du dashboard en lecture seule (tables `token_usage` et `events`) et le
journal local des agents (contracts/agents-journal.schema.json). Compteurs seuls : ni
prompt, ni contenu, ni identifiant de session dans le rapport. Stdlib ; Python 3.9.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_ROUTING = HERE / "config" / "model-routing.yml"
DEFAULT_JOURNAL = Path.home() / ".vva" / "agents-journal.jsonl"
MAX_LINES = 15
MAX_TICKETS = 7
MAIN_THREAD = "(fil principal)"
UTC = timezone.utc  # noqa: UP017 - pas de datetime.UTC en 3.9
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
BRANCH = re.compile(r"^(?:feat|fix|chore|docs|test|ci)/(\d+)-")
NAME = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
REASON = re.compile(r"^[\w .:-]{1,64}$")
TOOL_HOOKS = ("PostToolUse", "PostToolUseFailure")
GIT_TIMEOUT_S = 1.5


@dataclass(frozen=True)
class Routing:
    models: dict[str, str]
    default: str
    failures: int
    escalate_to: str
    labels: dict[str, str]
    agents: dict[str, str]


# --- routage --------------------------------------------------------------------------


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """« clé: valeur » et un niveau d'imbrication (2 espaces) ; commentaires `#`."""
    data: dict[str, Any] = {}
    section: dict[str, str] | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = "" if raw.lstrip().startswith("#") else raw.split(" #")[0].rstrip()
        if not line:
            continue
        key, sep, value = line.strip().partition(":")
        value = value.strip()
        if not sep or not key or line.startswith("   "):
            raise ValueError(f"routage, ligne {number} : « clé: valeur » attendu")
        if line.startswith("  ") and section is not None and value:
            section[key] = value
        elif line.startswith(" "):
            raise ValueError(f"routage, ligne {number} : indentation inattendue")
        elif value:
            data[key], section = value, None
        else:
            section = data[key] = {}
    return data


def load_routing(path: Path) -> Routing:
    data = parse_simple_yaml(path.read_text(encoding="utf-8"))
    escalation = data["escalation"]
    routing = Routing(
        models=data["models"],
        default=data["default"],
        failures=int(escalation["failures"]),
        escalate_to=escalation["model"],
        labels=data.get("labels", {}),
        agents=data.get("agents", {}),
    )
    used = [routing.default, routing.escalate_to, *routing.labels.values()]
    unknown = set(used + list(routing.agents.values())) - set(routing.models)
    if unknown:
        raise ValueError(f"routage : famille de modèle inconnue {sorted(unknown)}")
    return routing


def route(
    routing: Routing, agent: str, labels: Iterable[str] = (), failures: int = 0
) -> tuple[str, str | None]:
    """(modèle, motif de promotion ou None). Priorité : agent > label > escalade > défaut."""
    if agent in routing.agents:
        return routing.models[routing.agents[agent]], None
    for label in labels:
        if label in routing.labels:
            return routing.models[routing.labels[label]], label
    if failures >= routing.failures:
        return routing.models[routing.escalate_to], f"{failures} échecs"
    return routing.models[routing.default], None


# --- journal local ----------------------------------------------------------------------


def now() -> str:
    return datetime.now(UTC).isoformat()


def link_record(session_id: str, ticket: int, ts: str) -> dict[str, Any]:
    return {"kind": "link", "ts": ts, "session_id": session_id, "ticket": ticket}


def append_record(path: Path, record: dict[str, Any]) -> None:
    """Un seul write O_APPEND (hooks concurrents), fichier en 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def ticket_of_branch(branch: str) -> int | None:
    match = BRANCH.match(branch)
    return int(match.group(1)) if match else None


def _branch(cwd: str) -> str:
    out = subprocess.run(  # noqa: S603 - git local, cwd fourni par le hook
        ["git", "-C", cwd, "branch", "--show-current"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=True,
    )
    return out.stdout.strip()


def link(hook_text: str, journal: Path) -> None:
    """Hook SessionStart/UserPromptSubmit : session -> ticket de la branche. N'échoue jamais."""
    try:
        hook = json.loads(hook_text)
        session, cwd = hook["session_id"], hook["cwd"]
        if not (isinstance(session, str) and 0 < len(session) <= 128 and isinstance(cwd, str)):
            return
        ticket = ticket_of_branch(_branch(cwd))
        links = [r for r in read_records(journal) if r.get("kind") == "link"]
        last = [r["ticket"] for r in links if r.get("session_id") == session][-1:]
        if ticket is not None and last != [ticket]:
            append_record(journal, link_record(session, ticket, now()))
    except Exception:  # noqa: BLE001, S110 - un hook ne bloque jamais la session
        return


def failures_of(records: Sequence[dict[str, Any]], ticket: int, agent: str) -> int:
    """Échecs notés pour ce couple (ticket, agent) : un autre agent n'escalade pas celui-ci."""
    return sum(
        1
        for r in records
        if r.get("kind") == "fail" and r.get("ticket") == ticket and r.get("agent") == agent
    )


# --- lecture de la base ----------------------------------------------------------------


def _ts(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return EPOCH
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _query(db: Path, sql: str) -> list[tuple[Any, ...]]:
    if not db.is_file():
        raise SystemExit(f"token-report : base introuvable : {db}")
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        raise SystemExit(f"token-report : base illisible ({exc})") from exc
    finally:
        conn.close()


USAGE_SQL = (
    "SELECT session_id, ts, agent_id, model, SUM(count) FROM token_usage"
    " GROUP BY session_id, ts, agent_id, model"
)
EVENTS_SQL = (
    "SELECT session_id, ts, hook_event_name FROM events"
    " WHERE hook_event_name IN ('PostToolUse', 'PostToolUseFailure')"
)


class Tickets:
    """Ticket d'un instant d'une session : dernier rattachement antérieur, sinon le premier."""

    def __init__(self, records: Sequence[dict[str, Any]]) -> None:
        self.links: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
        for r in records:
            if r.get("kind") == "link" and isinstance(r.get("ticket"), int):
                self.links[str(r.get("session_id"))].append((_ts(str(r.get("ts"))), r["ticket"]))
        for links in self.links.values():
            links.sort()

    def at(self, session: str, ts: str) -> int | None:
        links = self.links.get(session)
        if not links:
            return None
        moment, chosen = _ts(ts), links[0][1]
        for when, ticket in links:
            if when > moment:
                break
            chosen = ticket
        return chosen


# --- rapport ------------------------------------------------------------------------------


def _fmt(n: float) -> str:
    return f"{round(n):,}".replace(",", " ")


def _name(value: str | None) -> str:
    if value is None:
        return MAIN_THREAD
    return value if NAME.match(value) else "?"


def _top(counter: Counter[str], limit: int, sep: str) -> str:
    items = [f"{k} {_fmt(v)}" for k, v in counter.most_common(limit)]
    rest = len(counter) - limit
    return sep.join(items) + (f"{sep}… +{rest}" if rest > 0 else "")


def _label(ticket: int | None) -> str:
    return f"#{ticket}" if ticket is not None else "sans ticket"


@dataclass
class Totals:
    tickets: dict[int | None, Counter[str]]
    agents: Counter[str]
    models: Counter[str]
    opus: set[tuple[int, str]]
    sessions: set[str]


def aggregate(rows: Sequence[tuple[Any, ...]], tickets: Tickets) -> Totals:
    totals = Totals(defaultdict(Counter), Counter(), Counter(), set(), set())
    for session, ts, agent, model, count in rows:
        ticket, who = tickets.at(session, ts), _name(agent)
        totals.tickets[ticket][who] += count
        totals.agents[who] += count
        totals.models[_name(model) if model else "?"] += count
        totals.sessions.add(session)
        if ticket is not None and agent is not None and "opus" in str(model) and who != "?":
            totals.opus.add((ticket, who))
    return totals


def _ticket_lines(totals: Totals) -> list[str]:
    ranked = sorted(
        ((t, c) for t, c in totals.tickets.items() if t is not None),
        key=lambda item: -sum(item[1].values()),
    )
    lines = [f"#{t} {_fmt(sum(c.values()))} : {_top(c, 4, ', ')}" for t, c in ranked[:MAX_TICKETS]]
    others = ranked[MAX_TICKETS:]
    if others:
        rest = sum(sum(c.values()) for _, c in others)
        lines.append(f"… {len(others)} autres tickets : {_fmt(rest)}")
    if None in totals.tickets:
        free = totals.tickets[None]
        lines.append(f"sans ticket {_fmt(sum(free.values()))} : {_top(free, 4, ', ')}")
    return lines


def _failure_line(rows: Sequence[tuple[Any, ...]], tickets: Tickets) -> str:
    calls: Counter[int | None] = Counter()
    failed: Counter[int | None] = Counter()
    for session, ts, hook in rows:
        ticket = tickets.at(session, ts)
        calls[ticket] += 1
        failed[ticket] += hook == "PostToolUseFailure"
    if not calls:
        return "Échecs d'outils : aucune donnée (hook PostToolUseFailure)"
    parts = [
        f"{_label(t)} {failed[t]}/{n} ({round(100 * failed[t] / n)} %)"
        for t, n in calls.most_common(5)
    ]
    return "Échecs d'outils : " + " · ".join(parts)


def _median_line(totals: Totals) -> str:
    sums = {t: sum(c.values()) for t, c in totals.tickets.items() if t is not None}
    if not sums:
        return "> 2× médiane : aucun ticket rattaché"
    median = statistics.median(sums.values())
    over = [f"#{t} ({_fmt(n)})" for t, n in sorted(sums.items()) if n > 2 * median]
    return f"> 2× médiane ({_fmt(median)}) : " + (", ".join(over) or "aucun")


def _promotion_line(totals: Totals, records: Sequence[dict[str, Any]], routing: Routing) -> str:
    traced: dict[tuple[int, str], str] = {}
    for r in records:
        if r.get("kind") != "promote" or type(r.get("ticket")) is not int:
            continue
        if "opus" in str(r.get("model")):
            reason = str(r.get("reason"))
            traced[(r["ticket"], _name(str(r.get("agent"))))] = (
                reason if REASON.match(reason) else "?"
            )
    for ticket, agent in sorted(totals.opus):
        if (ticket, agent) not in traced and "opus" not in route(routing, agent)[0]:
            traced[(ticket, agent)] = "non tracée"
    parts = [f"#{t} {a} ({why})" for (t, a), why in sorted(traced.items())]
    return "Promotions Opus : " + (" · ".join(parts[:6]) or "aucune")


def report(db: Path, journal: Path, routing: Routing) -> list[str]:
    records = read_records(journal)
    tickets = Tickets(records)
    totals = aggregate(_query(db, USAGE_SQL), tickets)
    attached = len([t for t in totals.tickets if t is not None])
    lines = [
        f"Tokens : {_fmt(sum(totals.agents.values()))} sur {attached} tickets"
        f" ({len(totals.sessions)} sessions), compteurs seuls",
        *_ticket_lines(totals),
        "Agents : " + _top(totals.agents, 6, " · "),
        "Modèles : " + _top(totals.models, 4, " · "),
        _failure_line(_query(db, EVENTS_SQL), tickets),
        _median_line(totals),
        _promotion_line(totals, records, routing),
    ]
    return lines[:MAX_LINES]


# --- ligne de commande --------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--journal", type=Path, default=_env_path("VVA_AGENTS_JOURNAL"))
    common.add_argument("--routing", type=Path, default=DEFAULT_ROUTING)
    parser = argparse.ArgumentParser(prog="token-report", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", parents=[common], help="rapport ≤ 15 lignes (défaut)")
    rep.add_argument("--db", type=Path, default=Path(os.environ.get("VVA_DB_PATH") or "vva.db"))
    sub.add_parser("link", parents=[common], help="hook : JSON du hook sur stdin")
    fail = sub.add_parser("fail", parents=[common], help="noter un échec sur un ticket")
    fail.add_argument("ticket", type=int)
    fail.add_argument("agent")
    rt = sub.add_parser("route", parents=[common], help="modèle à utiliser pour un agent")
    rt.add_argument("agent")
    rt.add_argument("--ticket", type=int)
    rt.add_argument("--label", action="append", default=[])
    return parser


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else DEFAULT_JOURNAL


def _name_arg(value: str, what: str) -> str:
    if not NAME.match(value):
        raise SystemExit(f"token-report : {what} invalide : {value!r}")
    return value


def _agent(value: str) -> str:
    return _name_arg(value, "nom d'agent")


def _ticket(value: int | None) -> int | None:
    if value is not None and value < 1:
        raise SystemExit(f"token-report : numéro de ticket invalide : {value}")
    return value


def _same_promotion(record: dict[str, Any], ticket: int, agent: str, reason: str) -> bool:
    return record.get("kind") == "promote" and (
        record.get("ticket"),
        record.get("agent"),
        record.get("reason"),
    ) == (ticket, agent, reason)


def _route(args: argparse.Namespace) -> list[str]:
    routing, agent, ticket = load_routing(args.routing), _agent(args.agent), _ticket(args.ticket)
    labels = [_name_arg(label, "label") for label in args.label]
    records = read_records(args.journal)
    failures = failures_of(records, ticket, agent) if ticket else 0
    model, reason = route(routing, agent, labels, failures)
    if reason is None or not ticket:
        return [model]
    if not any(_same_promotion(r, ticket, agent, reason) for r in records):
        record = {"kind": "promote", "ts": now(), "ticket": ticket, "agent": agent}
        append_record(args.journal, {**record, "model": model, "reason": reason})
    return [model]


def _fail(args: argparse.Namespace) -> list[str]:
    ticket, agent = _ticket(args.ticket), _agent(args.agent)
    append_record(args.journal, {"kind": "fail", "ts": now(), "ticket": ticket, "agent": agent})
    assert ticket is not None  # positionnel obligatoire
    total = failures_of(read_records(args.journal), ticket, agent)
    return [f"échec noté : #{ticket} {agent} ({total} pour cet agent sur ce ticket)"]


def run(argv: list[str]) -> list[str]:
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["report", *argv]
    args = _parser().parse_args(argv)
    if args.command == "link":
        link(sys.stdin.read(), args.journal)
        return []
    if args.command == "fail":
        return _fail(args)
    if args.command == "route":
        return _route(args)
    return report(args.db, args.journal, load_routing(args.routing))


def main() -> int:
    for line in run(sys.argv[1:]):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
