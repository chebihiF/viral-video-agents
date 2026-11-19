"""Accueil (#93) : agrège, sans recalcul, les sources existantes.

Agents : `live` (#101) ; tickets : `progress` (GitHub, #26) ; tokens et plafond : `tokens`,
`db.read_controls` ; fil : `delegations` (#102). Lecture seule (aucune mise en pause depuis un GET).
Le quota (#103) n'existe pas encore : tokens bruts, jamais de pourcentage sans légende.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dashboard import activity, auth, db, delegations, live, progress, tokens

FEED_SIZE = 5
log = logging.getLogger(__name__)
router = APIRouter()
auth.TEMPLATES.env.filters["thousands"] = tokens.thousands


@dataclass(frozen=True)
class Home:
    active: int
    waiting: int
    paused: bool
    pause_reason: str | None
    last_event: activity.Event | None
    tickets: progress.Counts | None  # None : GitHub injoignable
    tokens_24h: int
    tokens_7d: int
    cap_tokens: int
    cap_reached: bool
    feed: list[delegations.Delegation]
    last_id: int

    @property
    def empty(self) -> bool:
        return self.last_event is None and self.tokens_7d == 0


def _tickets() -> progress.Counts | None:
    snapshot, error_at = progress.CACHE.get()
    return None if error_at else progress.build_epics(snapshot)[0].counts


def _feed(conn: sqlite3.Connection, now: datetime) -> list[delegations.Delegation]:
    rows = [row for group in delegations.load_groups(conn, now) for row in group.rows]
    return sorted(rows, key=lambda row: row.at, reverse=True)[:FEED_SIZE]


def collect(now: datetime) -> Home:
    """Lève `sqlite3.Error` si la base est illisible ; GitHub en échec n'est jamais fatal."""
    with db.connect() as conn:
        last_id = activity.last_id(conn)  # avant la lecture : aucun trou possible
        rows = live.agent_rows(conn, now)
        events = activity.latest_events(conn, 1)
        paused, reason, cap, _ = db.read_controls(conn)
        report = tokens_report(conn, now)
        feed = _feed(conn, now)
    return Home(
        active=sum(row.state == "actif" for row in rows),
        waiting=sum(row.state == "en attente" for row in rows),
        paused=paused,
        pause_reason=reason,
        last_event=events[0] if events else None,
        tickets=_tickets(),
        tokens_24h=report[0],
        tokens_7d=report[1],
        cap_tokens=cap,
        cap_reached=report[0] >= cap,
        feed=feed,
        last_id=last_id,
    )


def tokens_report(conn: sqlite3.Connection, now: datetime) -> tuple[int, int]:
    """(tokens 24 h, tokens 7 j), même source que /tokens."""
    by_model = tokens.totals(conn, now).by_model
    return sum(t.last_24h for t in by_model), sum(t.last_7d for t in by_model)


def _page(request: Request, status: int, context: dict[str, object]) -> HTMLResponse:
    response = auth.TEMPLATES.TemplateResponse(request, "home.html", context, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


async def home_page(request: Request) -> Response:
    if not auth.is_authenticated(request):
        return RedirectResponse("/login", status_code=303, headers={"Cache-Control": "no-store"})
    try:
        data = await run_in_threadpool(collect, datetime.now(UTC))
    except sqlite3.Error as exc:
        log.warning("lecture de l'accueil impossible : %s", type(exc).__name__)
        failed_at = datetime.now(UTC).strftime("%H:%M:%S")
        return _page(request, 503, {"home": None, "error_at": failed_at})
    return _page(request, 200, {"home": data, "error_at": None})


router.add_api_route("/", home_page, methods=["GET"], response_model=None)
