"""GET /stream : flux SSE des événements d'agents (contrat : contracts/dashboard-routes.md).

`id` = rowid croissant de la table `events` : la reprise (`Last-Event-ID`) renvoie
exactement les événements d'id supérieur, sans perte ni doublon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse

from dashboard import activity, auth, db
from dashboard.activity import Event

POLL_S = 0.5  # latence maximale d'un événement ingéré (exigence : < 2 s)
STREAM_MAX_S = 300.0  # durée d'une connexion : le client reprend, la session est revérifiée
KEEPALIVE_EVERY = 30  # sondages à vide entre deux commentaires `: keepalive` (15 s)
BATCH = 100
RETRY_MS = 2000
MAX_ID_DIGITS = 18

_AFTER = (
    "SELECT rowid, event_id, ts, hook_event_name, agent_type, agent_id, tool_name "
    "FROM events WHERE rowid > ? ORDER BY rowid LIMIT ?"
)

log = logging.getLogger(__name__)
router = APIRouter()


def last_event_id() -> int:
    with db.connect() as conn:
        return activity.last_id(conn)


def events_after(after: int, limit: int = BATCH) -> list[tuple[int, Event]]:
    with db.connect() as conn:
        return [(row[0], Event(*row[1:])) for row in conn.execute(_AFTER, (after, limit))]


def format_sse(rowid: int, event: Event) -> str:
    """Message SSE : `data` est du JSON sur une ligne (aucune injection de champ possible)."""
    payload = asdict(event) | {"time_label": event.time_label}
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {rowid}\ndata: {data}\n\n"


async def event_stream(
    after: int, *, poll_s: float, max_polls: int, keepalive_every: int
) -> AsyncIterator[str]:
    """Émet `retry`, puis les événements d'id > `after` ; s'arrête après `max_polls` sondages."""
    yield f"retry: {RETRY_MS}\n\n"
    idle = 0
    for _ in range(max_polls):
        try:
            rows = await run_in_threadpool(events_after, after, BATCH)
        except sqlite3.Error as exc:
            log.warning("flux interrompu : %s", type(exc).__name__)
            return  # le client reprend avec Last-Event-ID
        for rowid, event in rows:
            yield format_sse(rowid, event)
            after = rowid
        if len(rows) == BATCH:
            continue  # il en reste : pas d'attente
        idle = 0 if rows else idle + 1
        if idle and idle % keepalive_every == 0:
            yield ": keepalive\n\n"
        await asyncio.sleep(poll_s)


def _parse_id(raw: str | None) -> int | None:
    if raw and raw.isascii() and raw.isdigit() and len(raw) <= MAX_ID_DIGITS:
        return int(raw)
    return None


async def _start_cursor(request: Request) -> int:
    """`Last-Event-ID` (reprise) > `?after=` (page rendue) > dernier id connu (rien à rejouer)."""
    for raw in (request.headers.get("last-event-id"), request.query_params.get("after")):
        cursor = _parse_id(raw)
        if cursor is not None:
            return cursor
    return await run_in_threadpool(last_event_id)


@router.get("/stream", response_model=None)
async def stream(request: Request) -> Response:
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    if not auth.is_authenticated(request):
        return Response(status_code=401, headers={"Cache-Control": "no-store"})
    try:
        after = await _start_cursor(request)
    except sqlite3.Error as exc:
        log.warning("lecture du curseur impossible : %s", type(exc).__name__)
        return Response(status_code=503, headers={"Cache-Control": "no-store"})
    body = event_stream(
        after,
        poll_s=POLL_S,
        max_polls=max(1, int(STREAM_MAX_S / POLL_S)),
        keepalive_every=KEEPALIVE_EVERY,
    )
    return StreamingResponse(body, media_type="text/event-stream", headers=headers)
