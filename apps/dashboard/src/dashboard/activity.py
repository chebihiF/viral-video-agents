"""GET /activity : 100 derniers événements d'agents (contrat : contracts/dashboard-routes.md)."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dashboard import auth, db

MAX_EVENTS = 100
_SELECT = (
    "SELECT event_id, ts, hook_event_name, agent_type, agent_id, tool_name "
    "FROM events ORDER BY ts DESC, rowid DESC LIMIT ?"
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


def agents(events: list[Event]) -> list[Event]:
    """Dernier événement de chaque agent (`events` est trié du plus récent au plus ancien)."""
    latest: dict[tuple[str | None, str | None], Event] = {}
    for event in events:
        latest.setdefault((event.agent_type, event.agent_id), event)
    return list(latest.values())


def load_events() -> list[Event]:
    with db.connect() as conn:
        return latest_events(conn)


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
        events = await run_in_threadpool(load_events)
    except sqlite3.Error as exc:
        log.warning("lecture des événements impossible : %s", type(exc).__name__)
        failed_at = datetime.now(UTC).strftime("%H:%M:%S")
        return _page(request, 503, {"error_at": failed_at, "events": [], "agents": []})
    return _page(request, 200, {"error_at": None, "events": events, "agents": agents(events)})
