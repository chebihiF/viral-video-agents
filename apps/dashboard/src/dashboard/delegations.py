"""Délégations (#102) : qui lance quel sous-agent, dans l'ordre, groupé par ticket.

Une délégation = un `PreToolUse` d'outil de délégation (`Task`/`Agent`) suivi, dans la même
session et dans `PAIR_WINDOW`, du `SubagentStart` d'un `agent_type`. L'événement d'outil sans
`agent_id` vient du fil principal (« tech lead ») ; sinon le délégant est son `agent_type`.
Appariement dans l'ordre d'arrivée (le `SubagentStart` ne référence pas l'appel d'outil) : en
cas de lancements parallèles l'ordre est conservé, pas l'identité de l'appel. Le ticket vient
de la colonne `ticket` (#100) ; vide, il est inconnu. Métadonnées uniquement.
`ORDER BY ts` suppose des heures au même décalage (+00:00, comme l'ingestion).
"""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dashboard.live import format_duration

DELEGATION_TOOLS = ("Task", "Agent")
START, STOP, TOOL_EVENT = "SubagentStart", "SubagentStop", "PreToolUse"
MAIN_THREAD = "tech lead"
UNKNOWN_AGENT = "agent inconnu"
PAIR_WINDOW = timedelta(minutes=2)  # délai max entre l'appel d'outil et le SubagentStart
HISTORY_WINDOW = timedelta(days=7)  # borne la lecture (index idx_events_ts)
MAX_EVENTS = 2000
MAX_ROWS = 50

_SELECT = (
    "SELECT ts, hook_event_name, session_id, agent_type, agent_id, ticket FROM events "
    "WHERE ts >= ? AND (hook_event_name IN (?, ?) "
    "OR (hook_event_name = ? AND tool_name IN (?, ?))) "
    "ORDER BY ts DESC, rowid DESC LIMIT ?"
)
Raw = tuple[str | None, ...]


@dataclass(frozen=True)
class Delegation:
    at: datetime
    delegator: str
    delegatee: str
    ticket: int | None
    duration: str

    @property
    def line(self) -> str:
        ticket = "ticket inconnu" if self.ticket is None else f"ticket #{self.ticket}"
        return f"{self.at.strftime('%H:%M')} {self.delegator} → {self.delegatee} ({ticket})"


@dataclass(frozen=True)
class TicketGroup:
    ticket: int | None
    rows: list[Delegation]

    @property
    def title(self) -> str:
        return "Ticket inconnu" if self.ticket is None else f"Ticket #{self.ticket}"


@dataclass
class _Open:
    at: datetime
    delegator: str
    delegatee: str
    ticket: int | None
    started: datetime
    stopped: datetime | None = None

    def freeze(self) -> Delegation:
        if self.stopped is None:
            duration = "en cours"
        else:
            duration = f"a duré {format_duration(self.stopped - self.started)}"
        return Delegation(self.at, self.delegator, self.delegatee, self.ticket, duration)


def _parse(ts: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) if parsed else None


def _ticket(raw: object) -> int | None:
    """Numéro de ticket tolérant : « #93 », « 93a », 93.7 ou inf sont inconnus."""
    if isinstance(raw, float) and not raw.is_integer():
        return None
    try:
        return int(raw) if raw is not None else None  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return None


def _delegator(agent_type: str | None, agent_id: str | None) -> str:
    return (agent_type or agent_id or UNKNOWN_AGENT) if agent_id else MAIN_THREAD


def _fresh(pending: deque[tuple[datetime, str, int | None]], now: datetime) -> None:
    while pending and now - pending[0][0] > PAIR_WINDOW:
        pending.popleft()


def pair(raws: list[Raw]) -> list[_Open]:
    """Apparie, dans l'ordre chronologique, appels d'outil, SubagentStart puis SubagentStop."""
    pending: dict[str, deque[tuple[datetime, str, int | None]]] = {}
    running: dict[tuple[str, str], _Open] = {}
    found: list[_Open] = []
    for ts, hook, session, agent_type, agent_id, ticket in raws:
        when = _parse(ts)
        if when is None or session is None:
            continue
        queue = pending.setdefault(session, deque())
        if hook == TOOL_EVENT:
            queue.append((when, _delegator(agent_type, agent_id), _ticket(ticket)))
        elif hook == START:
            _fresh(queue, when)
            if not queue:
                continue
            at, delegator, tool_ticket = queue.popleft()
            number = tool_ticket if tool_ticket is not None else _ticket(ticket)
            item = _Open(at, delegator, agent_type or UNKNOWN_AGENT, number, when)
            found.append(item)
            if agent_id:
                running[(session, agent_id)] = item
        elif (opened := running.pop((session, agent_id or ""), None)) is not None:
            opened.stopped = when
    return found


def load_groups(conn: sqlite3.Connection, now: datetime) -> list[TicketGroup]:
    """Délégations des 7 derniers jours (50 au plus récentes), groupées par ticket."""
    since = (now - HISTORY_WINDOW).astimezone(UTC).isoformat()
    args = (since, START, STOP, TOOL_EVENT, *DELEGATION_TOOLS, MAX_EVENTS)
    raws = list(reversed(conn.execute(_SELECT, args).fetchall()))
    rows = [item.freeze() for item in pair(raws)][-MAX_ROWS:]
    by_ticket: dict[int | None, list[Delegation]] = {}
    for row in rows:
        by_ticket.setdefault(row.ticket, []).append(row)
    ordered = sorted(by_ticket.items(), key=lambda kv: kv[1][-1].at, reverse=True)
    return [TicketGroup(ticket, group) for ticket, group in ordered]
