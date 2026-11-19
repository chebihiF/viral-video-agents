"""GET /activity : 100 derniers événements d'agents (contrat : contracts/dashboard-routes.md)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dashboard import auth, db, live

MAX_EVENTS = 100
_SELECT = (
    "SELECT event_id, ts, hook_event_name, agent_type, agent_id, tool_name "
    "FROM events ORDER BY ts DESC, rowid DESC LIMIT ?"
)
_LAST_ID = "SELECT COALESCE(MAX(rowid), 0) FROM events"

HISTORY_DAYS = 7
HISTORY_FETCH = 500  # sessions (agent, ticket) lues avant filtre
HISTORY_SHOWN = 100
FAILURE_EVENT = "PostToolUseFailure"  # hook émis quand un outil échoue
STATUSES = ("actif", "en attente", "terminé", "erreur")
_HISTORY = (
    "SELECT agent_type, agent_id, ticket, MIN(ts), MAX(ts), "
    "MAX(CASE WHEN hook_event_name = ? THEN ts END), "
    "SUM(hook_event_name = ?), "
    "(SELECT SUM(t.count) FROM token_usage t WHERE t.agent_id = e.agent_id "
    "AND t.ts >= MIN(e.ts) AND t.ts <= MAX(e.ts)) "
    "FROM events e WHERE ts >= ? GROUP BY agent_type, agent_id, ticket "
    "ORDER BY MAX(ts) DESC LIMIT ?"
)

log = logging.getLogger(__name__)
router = APIRouter()


@dataclass(frozen=True)
class Event:
    event_id: str
    ts: str
    hook_event_name: str
    agent_type: str | None
    agent_id: str | None
    tool_name: str | None

    @property
    def time_label(self) -> str:
        try:
            return datetime.fromisoformat(self.ts).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return self.ts


def latest_events(conn: sqlite3.Connection, limit: int = MAX_EVENTS) -> list[Event]:
    return [Event(*row) for row in conn.execute(_SELECT, (limit,))]


def last_id(conn: sqlite3.Connection) -> int:
    """Identifiant du dernier événement : curseur de départ du flux SSE (`/stream`)."""
    return int(conn.execute(_LAST_ID).fetchone()[0])


def agents(events: list[Event]) -> list[Event]:
    """Dernier événement de chaque agent (`events` est trié du plus récent au plus ancien)."""
    latest: dict[tuple[str | None, str | None], Event] = {}
    for event in events:
        latest.setdefault((event.agent_type, event.agent_id), event)
    return list(latest.values())


@dataclass(frozen=True)
class HistoryRow:
    agent: str
    ticket: int | None
    state: str
    duration: str
    minutes: int
    errors: int
    tokens: int | None
    last_label: str


class Filters(NamedTuple):
    agent: str | None = None
    status: str | None = None
    min_minutes: int | None = None

    @property
    def active(self) -> list[str]:
        labels = []
        if self.agent:
            labels.append(f"agent : {self.agent}")
        if self.status:
            labels.append(f"statut : {self.status}")
        if self.min_minutes:
            labels.append(f"durée d'au moins {self.min_minutes} min")
        return labels


def _is_ascii_digits(raw: str) -> bool:
    """`str.isdigit()` accepte « ² » (int() échoue) : on exige des chiffres 0-9."""
    return raw.isascii() and raw.isdigit()


def parse_filters(params: Mapping[str, str]) -> Filters:
    """Filtres de l'URL ; une valeur invalide est ignorée (jamais d'erreur ni d'écho brut)."""
    status = params.get("statut")
    minutes = params.get("duree_min", "")
    return Filters(
        agent=params.get("agent") or None,
        status=status if status in STATUSES else None,
        min_minutes=int(minutes) if _is_ascii_digits(minutes) and int(minutes) < 10**6 else None,
    )


def _history_row(raw: tuple[Any, ...], now: datetime) -> HistoryRow | None:
    agent_type, agent_id, ticket, first_ts, last_ts, stop_ts, failures, tokens = raw
    first, last = live.parse_ts(first_ts), live.parse_ts(last_ts)
    if first is None or last is None:
        return None
    state = live.state_of(now, last, live.parse_ts(stop_ts))
    span = last - first if state == "terminé" else now - first
    prefix = "a duré" if state == "terminé" else "depuis"
    return HistoryRow(
        agent=agent_type or agent_id or live.UNKNOWN_AGENT,
        ticket=live.ticket_number(ticket),
        state=state,
        duration=f"{prefix} {live.format_duration(span)}",
        minutes=max(int(span.total_seconds()) // 60, 0),
        errors=int(failures or 0),
        tokens=int(tokens) if tokens else None,
        last_label=last.astimezone(UTC).strftime("%Y-%m-%d %H:%M"),
    )


def _matches(row: HistoryRow, f: Filters) -> bool:
    if f.agent and row.agent != f.agent:
        return False
    if f.status == "erreur" and not row.errors:
        return False
    if f.status and f.status != "erreur" and row.state != f.status:
        return False
    return f.min_minutes is None or row.minutes >= f.min_minutes


def history(
    conn: sqlite3.Connection, now: datetime, f: Filters
) -> tuple[list[HistoryRow], list[str]]:
    """Sessions (agent, ticket) des 7 derniers jours, filtrées ; et noms d'agents pour le filtre."""
    since = (now - timedelta(days=HISTORY_DAYS)).astimezone(UTC).isoformat()
    args = (live.STOP_EVENT, FAILURE_EVENT, since, HISTORY_FETCH)
    rows = [r for raw in conn.execute(_HISTORY, args) if (r := _history_row(raw, now))]
    names = sorted({r.agent for r in rows})
    return [r for r in rows if _matches(r, f)][:HISTORY_SHOWN], names


def load_history(now: datetime, f: Filters) -> tuple[list[HistoryRow], list[str]]:
    with db.connect() as conn:
        return history(conn, now, f)


def load_events() -> list[Event]:
    with db.connect() as conn:
        return latest_events(conn)


def load_last_id() -> int:
    with db.connect() as conn:
        return last_id(conn)


def _page(request: Request, status: int, context: dict[str, object]) -> HTMLResponse:
    response = auth.TEMPLATES.TemplateResponse(
        request, "activity.html", context, status_code=status
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/activity", response_model=None)
async def activity_page(request: Request) -> Response:
    if not auth.is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    try:
        cursor = await run_in_threadpool(load_last_id)  # avant la lecture : aucun trou possible
        events = await run_in_threadpool(load_events)
        now = datetime.now(UTC)
        live_rows = await run_in_threadpool(live.load_rows, now)
        filters = parse_filters(request.query_params)
        history_rows, agent_names = await run_in_threadpool(load_history, now, filters)
    except sqlite3.Error as exc:
        log.warning("lecture des événements impossible : %s", type(exc).__name__)
        failed_at = datetime.now(UTC).strftime("%H:%M:%S")
        error: dict[str, object] = {
            "error_at": failed_at,
            "events": [],
            "agents": [],
            "live_rows": [],
            "history_rows": [],
            "last_id": None,
        }
        return _page(request, 503, error)
    context: dict[str, object] = {
        "error_at": None,
        "events": events,
        "agents": agents(events),
        "live_rows": live_rows,
        "history_rows": history_rows,
        "agent_names": agent_names,
        "filters": filters,
        "last_id": cursor,
    }
    return _page(request, 200, context)
