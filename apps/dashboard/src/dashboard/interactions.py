"""GET /interactions : sessions, sous-agents et durée (contrat : contracts/dashboard-routes.md)."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dashboard import activity, auth, db, delegations
from dashboard.live import format_duration

MAX_SESSIONS = 20
MAX_TICKETS = 30
TICKET_WINDOW = delegations.HISTORY_WINDOW
ISSUES_URL = "https://github.com/chebihiF/viral-video-agents/issues"
START, STOP = "SubagentStart", "SubagentStop"
_SESSIONS = (
    "SELECT session_id, MIN(ts), MAX(ts), COUNT(*) FROM events "
    "GROUP BY session_id ORDER BY MAX(ts) DESC, MAX(rowid) DESC LIMIT ?"
)
_SUBAGENT_EVENTS = (
    "SELECT session_id, agent_id, agent_type, hook_event_name, ts FROM events "
    "WHERE hook_event_name IN (?, ?) AND agent_id IS NOT NULL "
    "AND session_id IN ({marks}) ORDER BY ts, rowid"
)
_TICKETS = (
    "SELECT ticket, MIN(ts), MAX(ts), COUNT(DISTINCT session_id), "
    "COUNT(DISTINCT CASE WHEN hook_event_name = ? THEN session_id || '/' || agent_id END) "
    "FROM events WHERE ticket IS NOT NULL AND ts >= ? GROUP BY ticket"
)
_UNTRACKED = "SELECT COUNT(*) FROM events WHERE ticket IS NULL AND ts >= ?"

log = logging.getLogger(__name__)
router = APIRouter()


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts).astimezone(UTC) if ts else None
    except ValueError:
        return None


def _label(ts: str | None) -> str:
    parsed = _parse(ts)
    return parsed.strftime("%H:%M:%S") if parsed else "—"


@dataclass(frozen=True)
class Subagent:
    agent_id: str
    agent_type: str | None
    started_at: str | None
    stopped_at: str | None

    @property
    def duration_s(self) -> float | None:
        start, stop = _parse(self.started_at), _parse(self.stopped_at)
        if start is None or stop is None:
            return None
        return max(0.0, (stop - start).total_seconds())

    @property
    def running(self) -> bool:
        return self.started_at is not None and self.stopped_at is None

    @property
    def duration_label(self) -> str:
        seconds = self.duration_s
        if seconds is None:
            return "—"
        whole = int(seconds)
        if whole < 1:
            return "< 1 s"
        if whole < 60:
            return f"{whole} s"
        if whole < 3600:
            return f"{whole // 60} min {whole % 60:02d} s"
        return f"{whole // 3600} h {whole % 3600 // 60:02d} min"

    @property
    def start_label(self) -> str:
        return _label(self.started_at)


@dataclass(frozen=True)
class Session:
    session_id: str
    first_ts: str
    last_ts: str
    event_count: int
    subagents: list[Subagent] = field(default_factory=list)

    @property
    def first_label(self) -> str:
        return _label(self.first_ts)

    @property
    def last_label(self) -> str:
        return _label(self.last_ts)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count > 1 else ''}"


@dataclass(frozen=True)
class TicketStat:
    """Durée (première à dernière activité), sous-agents lancés et sessions d'un ticket."""

    ticket: int
    duration_s: int
    subagents: int
    sessions: int

    @property
    def duration_label(self) -> str:
        if self.duration_s < 60:
            return "< 1 min"
        return format_duration(timedelta(seconds=self.duration_s))

    @property
    def url(self) -> str:
        return f"{ISSUES_URL}/{self.ticket}"

    @property
    def line(self) -> str:
        return (
            f"ticket #{self.ticket} · durée {self.duration_label} · "
            f"{_plural(self.subagents, 'sous-agent')} · {_plural(self.sessions, 'session')}"
        )


def load_ticket_stats(conn: sqlite3.Connection, now: datetime) -> list[TicketStat]:
    """Tickets des 7 derniers jours, du plus long au plus court (les sans-ticket sont exclus)."""
    since = (now - TICKET_WINDOW).astimezone(UTC).isoformat()
    stats: list[TicketStat] = []
    for ticket, first, last, sessions, subagents in conn.execute(_TICKETS, (START, since)):
        begin, end = _parse(first), _parse(last)
        seconds = int((end - begin).total_seconds()) if begin and end else 0
        stats.append(TicketStat(int(ticket), max(seconds, 0), subagents, sessions))
    stats.sort(key=lambda stat: (-stat.duration_s, -stat.ticket))
    return stats[:MAX_TICKETS]


def count_untracked(conn: sqlite3.Connection, now: datetime) -> int:
    """Événements des 7 derniers jours sans numéro de ticket (exclus du tableau)."""
    since = (now - TICKET_WINDOW).astimezone(UTC).isoformat()
    return int(conn.execute(_UNTRACKED, (since,)).fetchone()[0])


def _pair_subagents(rows: list[tuple[str, str, str | None, str, str]]) -> dict[str, list[Subagent]]:
    """Apparie Start/Stop par (session, agent_id) : premier Start, dernier Stop."""
    merged: dict[tuple[str, str], Subagent] = {}
    for session_id, agent_id, agent_type, name, ts in rows:
        key = (session_id, agent_id)
        current = merged.get(key) or Subagent(agent_id, agent_type, None, None)
        kind = agent_type or current.agent_type
        if name == START:
            merged[key] = Subagent(agent_id, kind, current.started_at or ts, current.stopped_at)
        else:
            merged[key] = Subagent(agent_id, kind, current.started_at, ts)
    by_session: dict[str, list[Subagent]] = {}
    for (session_id, _), subagent in merged.items():
        by_session.setdefault(session_id, []).append(subagent)
    return by_session


def load_sessions(conn: sqlite3.Connection) -> list[Session]:
    heads = conn.execute(_SESSIONS, (MAX_SESSIONS,)).fetchall()
    if not heads:
        return []
    ids = [row[0] for row in heads]
    query = _SUBAGENT_EVENTS.format(marks=", ".join("?" for _ in ids))  # noqa: S608 - marqueurs
    paired = _pair_subagents(conn.execute(query, (START, STOP, *ids)).fetchall())
    return [
        Session(sid, first, last, count, paired.get(sid, [])) for sid, first, last, count in heads
    ]


@dataclass(frozen=True)
class Page:
    cursor: int
    sessions: list[Session]
    groups: list[delegations.TicketGroup]
    tickets: list[TicketStat]
    untracked: int


def load_page() -> Page:
    with db.connect() as conn:
        cursor = activity.last_id(conn)  # avant la lecture : aucun trou possible
        sessions = load_sessions(conn)
        now = datetime.now(UTC)
        groups = delegations.load_groups(conn, now)
        tickets = load_ticket_stats(conn, now)
        return Page(cursor, sessions, groups, tickets, count_untracked(conn, now))


def _page(request: Request, status: int, context: dict[str, object]) -> HTMLResponse:
    response = auth.TEMPLATES.TemplateResponse(
        request, "interactions.html", context, status_code=status
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/interactions", response_model=None)
async def interactions_page(request: Request) -> Response:
    if not auth.is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    try:
        page = await run_in_threadpool(load_page)
    except sqlite3.Error as exc:
        log.warning("lecture des interactions impossible : %s", type(exc).__name__)
        failed_at = datetime.now(UTC).strftime("%H:%M:%S")
        return _page(
            request,
            503,
            {
                "error_at": failed_at,
                "sessions": [],
                "delegation_groups": [],
                "ticket_stats": [],
                "untracked": 0,
                "last_id": None,
            },
        )
    context: dict[str, object] = {
        "error_at": None,
        "sessions": page.sessions,
        "delegation_groups": page.groups,
        "ticket_stats": page.tickets,
        "untracked": page.untracked,
        "last_id": page.cursor,
    }
    return _page(request, 200, context)
