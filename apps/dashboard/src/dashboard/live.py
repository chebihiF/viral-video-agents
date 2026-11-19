"""« Qui fait quoi » (#101) : un état par agent et par ticket, dérivé de la table `events`.

États : actif (dernier événement < 2 min), en attente (2 à 15 min), terminé (dernier événement
`SubagentStop`, ou plus de 15 min). Le ticket vient de la colonne `ticket` (#100) ;
si elle est vide, il est inconnu. Métadonnées uniquement : aucun contenu de prompt.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dashboard import db

MAX_ROWS = 20
ACTIVE_WINDOW = timedelta(minutes=2)
WAITING_WINDOW = timedelta(minutes=15)
HISTORY_WINDOW = timedelta(hours=24)  # borne la lecture (index idx_events_ts)
STOP_EVENT = "SubagentStop"
UNKNOWN_AGENT = "agent inconnu"

_SELECT = (
    "SELECT agent_type, agent_id, ticket, MIN(ts), MAX(ts), "
    "MAX(CASE WHEN hook_event_name = ? THEN ts END) "
    "FROM events WHERE ts >= ? GROUP BY agent_type, agent_id, ticket "
    "ORDER BY MAX(ts) DESC LIMIT ?"
)


@dataclass(frozen=True)
class LiveRow:
    agent: str
    ticket: int | None
    state: str
    duration: str
    last_label: str


def parse_ts(ts: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def format_duration(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds()) // 60, 0)
    if minutes < 1:
        return "moins d'une minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {rest:02d}"
    days, hours = divmod(hours, 24)
    return f"{days} j {hours} h"


def state_of(now: datetime, last: datetime, stopped_at: datetime | None) -> str:
    if stopped_at is not None and stopped_at >= last:
        return "terminé"
    age = now - last
    if age < ACTIVE_WINDOW:
        return "actif"
    return "en attente" if age <= WAITING_WINDOW else "terminé"


def ticket_number(raw: object) -> int | None:
    """Numéro de ticket tolérant : une valeur non numérique (« #93 », « 93a ») est inconnue."""
    try:
        return int(raw) if raw is not None else None  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _row(raw: tuple[str | None, ...], now: datetime) -> LiveRow | None:
    agent_type, agent_id, ticket, first_ts, last_ts, stop_ts = raw
    first, last = parse_ts(first_ts), parse_ts(last_ts)
    if first is None or last is None:
        return None
    state = state_of(now, last, parse_ts(stop_ts))
    duration = (
        f"a duré {format_duration(last - first)}"
        if state == "terminé"
        else f"depuis {format_duration(now - first)}"
    )
    return LiveRow(
        agent=agent_type or agent_id or UNKNOWN_AGENT,
        ticket=ticket_number(ticket),
        state=state,
        duration=duration,
        last_label=last.astimezone(UTC).strftime("%H:%M"),
    )


def agent_rows(conn: sqlite3.Connection, now: datetime) -> list[LiveRow]:
    """Une ligne par (agent, ticket), la plus récente d'abord ; `now` est injecté."""
    since = (now - HISTORY_WINDOW).astimezone(UTC).isoformat()
    rows = (_row(raw, now) for raw in conn.execute(_SELECT, (STOP_EVENT, since, MAX_ROWS)))
    return [row for row in rows if row is not None]


def load_rows(now: datetime) -> list[LiveRow]:
    with db.connect() as conn:
        return agent_rows(conn, now)
