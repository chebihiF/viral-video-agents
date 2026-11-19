"""Pause des agents et plafond de tokens (contrat : contracts/control-api.md).

Pages : GET /controls et POST /controls/{pause,resume,cap} (session + jeton CSRF, fermées si la
base est illisible). API :
GET /api/v1/control (jeton Bearer d'ingestion), lue par le hook PreToolUse.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from dashboard import auth, db, ingest, live, quota, quota_view, tokens

REASON_MANUAL = "manuel"
REASON_CAP = "plafond"
MAX_FORM_BYTES = 4096
MAX_CAP_TOKENS = 10**12
HISTORY_DAYS = 7
HISTORY_SHOWN = 20
HISTORY_EVENTS = ("SubagentStart", "SubagentStop", "PostToolUseFailure")
_HISTORY = (
    "SELECT ts, hook_event_name, agent_type, agent_id, tool_name, ticket FROM events "
    "WHERE ts >= ? AND hook_event_name IN (?, ?, ?) ORDER BY ts DESC, rowid DESC LIMIT ?"
)

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
TEMPLATES.env.filters["thousands"] = tokens.thousands
log = logging.getLogger(__name__)
router = APIRouter()


class ControlState(BaseModel):
    """Corps de GET /api/v1/control (documenté dans contracts/control-api.md)."""

    paused: bool
    reason: str | None
    cap_tokens: int
    tokens_24h: int
    updated_at: str


@dataclass(frozen=True)
class Refusal:
    status: int
    message: str


Action = Callable[[sqlite3.Connection, dict[str, str]], Refusal | None]


def tokens_24h(conn: sqlite3.Connection, now: datetime) -> int:
    """Total des tokens des dernières 24 h glissantes (même source que /tokens, #7)."""
    return sum(total.last_24h for total in tokens.totals(conn, now).by_model)


def current_state(conn: sqlite3.Connection, now: datetime) -> ControlState:
    """État courant ; le plafond atteint met en pause (motif « plafond ») avant de répondre."""
    paused, reason, cap, updated_at = db.read_controls(conn)
    used = tokens_24h(conn, now)
    if not paused and used >= cap:
        db.pause(conn, REASON_CAP)
        paused, reason, cap, updated_at = db.read_controls(conn)
    return ControlState(
        paused=paused, reason=reason, cap_tokens=cap, tokens_24h=used, updated_at=updated_at
    )


@dataclass(frozen=True)
class AgentRow:
    agent: str
    ticket: int | None
    state: str  # « actif », « en attente » ou « en pause » (pause globale)


@dataclass(frozen=True)
class HistoryRow:
    date: str
    action: str
    result: str


@dataclass(frozen=True)
class PageData:
    state: ControlState
    agents: list[AgentRow]
    history: list[HistoryRow]
    quota_5h: quota_view.Gauge
    quota_configured: bool


def agents_now(conn: sqlite3.Connection, now: datetime, paused: bool) -> list[AgentRow]:
    """Agents non terminés (#101) ; en pause globale, tous sont « en pause »."""
    rows = [row for row in live.agent_rows(conn, now) if row.state != "terminé"]
    return [AgentRow(r.agent, r.ticket, "en pause" if paused else r.state) for r in rows]


def _history_row(raw: tuple[str | None, ...]) -> HistoryRow | None:
    ts, hook, agent_type, agent_id, tool, ticket = raw
    at = live.parse_ts(ts)
    if at is None:
        return None
    who = agent_type or agent_id or live.UNKNOWN_AGENT
    number = live.ticket_number(ticket)
    where = "" if number is None else f" (ticket #{number})"
    date = at.astimezone(UTC).strftime("%Y-%m-%d %H:%M") + " UTC"
    if hook == "SubagentStart":
        return HistoryRow(date, f"{who} démarre{where}", "lancé")
    if hook == "SubagentStop":
        return HistoryRow(date, f"{who} se termine{where}", "terminé")
    return HistoryRow(date, f"{who} : outil {tool or 'inconnu'} en échec{where}", "échec")


def history(conn: sqlite3.Connection, now: datetime) -> list[HistoryRow]:
    """Dernières actions des agents (démarrage, fin, échec d'outil), la plus récente en haut."""
    since = (now - timedelta(days=HISTORY_DAYS)).astimezone(UTC).isoformat()
    cursor = conn.execute(_HISTORY, (since, *HISTORY_EVENTS, HISTORY_SHOWN))
    rows = (_history_row(raw) for raw in cursor)
    return [row for row in rows if row is not None]


def load_page(now: datetime) -> PageData:
    with db.connect() as conn:
        state = current_state(conn, now)
        configured = quota.read_quota()
        view = quota_view.build(conn, now, configured)
        return PageData(
            state=state,
            agents=agents_now(conn, now, state.paused),
            history=history(conn, now),
            quota_5h=view.five_hours,
            quota_configured=configured.five_hours is not None,
        )


def load_state() -> ControlState:
    with db.connect() as conn:
        return current_state(conn, datetime.now(UTC))


# --- CSRF : jeton lié au cookie de session, vérifié en temps constant ---------------------------
def csrf_token(key: str, session_cookie: str) -> str:
    mac = hmac.new(key.encode(), f"csrf.v1.{session_cookie}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def _session(request: Request) -> str | None:
    """Jeton CSRF de la session ouverte ; None sans session valide."""
    if not auth.is_authenticated(request):
        return None
    cookie = request.cookies.get(auth.COOKIE_NAME, "")
    return csrf_token(os.environ.get(auth.SESSION_KEY_ENV, ""), cookie)


async def _read_form(request: Request) -> dict[str, str] | None:
    """Formulaire urlencoded borné (champ -> première valeur) ; None si trop gros."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FORM_BYTES:
            return None
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {name: values[0] for name, values in parsed.items()}


# --- Pages ----------------------------------------------------------------------------------------
def _page(request: Request, csrf: str, status: int, message: str | None = None) -> HTMLResponse:
    context: dict[str, object] = {"csrf": csrf, "message": message}
    try:
        data = load_page(datetime.now(UTC))
        context.update(state=data.state, agents=data.agents, history=data.history, quota=data)
    except sqlite3.Error as exc:
        log.warning("lecture des contrôles impossible : %s", type(exc).__name__)
        context.update(state=None, message=None, error_at=datetime.now(UTC).strftime("%H:%M:%S"))
        status = 503
    response = TEMPLATES.TemplateResponse(request, "controls.html", context, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/controls", response_model=None)
async def controls_page(request: Request) -> Response:
    csrf = _session(request)
    if csrf is None:
        return RedirectResponse("/login", status_code=303)
    return await run_in_threadpool(_page, request, csrf, 200)


def _pause(conn: sqlite3.Connection, _fields: dict[str, str]) -> Refusal | None:
    db.pause(conn, REASON_MANUAL)
    return None


def _resume(conn: sqlite3.Connection, _fields: dict[str, str]) -> Refusal | None:
    state = current_state(conn, datetime.now(UTC))
    if state.tokens_24h >= state.cap_tokens:
        return Refusal(409, "Plafond atteint : relevez le plafond avant de reprendre.")
    db.resume(conn)
    return None


def _parse_cap(raw: str) -> int | None:
    if not (raw.isascii() and raw.isdigit()) or len(raw) > len(str(MAX_CAP_TOKENS)):
        return None
    value = int(raw)
    return value if 0 < value <= MAX_CAP_TOKENS else None


def _set_cap(conn: sqlite3.Connection, fields: dict[str, str]) -> Refusal | None:
    cap = _parse_cap(fields.get("cap_tokens", ""))
    if cap is None:
        return Refusal(422, "Plafond invalide : entier entre 1 et 1 000 000 000 000.")
    db.set_cap(conn, cap)
    current_state(conn, datetime.now(UTC))  # un plafond abaissé sous la conso met en pause
    return None


def _apply(action: Action, fields: dict[str, str]) -> Refusal | None:
    with db.connect() as conn:
        return action(conn, fields)


async def _handle(request: Request, action: Action) -> Response:
    """Ordre : session, taille, CSRF, puis seulement la base (fermée en 503 si illisible)."""
    csrf = _session(request)
    if csrf is None:
        return RedirectResponse("/login", status_code=303)
    fields = await _read_form(request)
    if fields is None:
        raise HTTPException(status_code=413, detail=f"corps limité à {MAX_FORM_BYTES} octets")
    if not hmac.compare_digest(fields.get("csrf", "").encode(), csrf.encode()):
        raise HTTPException(status_code=403, detail="jeton CSRF invalide")
    try:
        refusal = await run_in_threadpool(_apply, action, fields)
    except sqlite3.Error:
        return await run_in_threadpool(_page, request, csrf, 503)
    if refusal is not None:
        return await run_in_threadpool(_page, request, csrf, refusal.status, refusal.message)
    return RedirectResponse("/controls", status_code=303)


@router.post("/controls/pause", response_model=None)
async def pause_agents(request: Request) -> Response:
    return await _handle(request, _pause)


@router.post("/controls/resume", response_model=None)
async def resume_agents(request: Request) -> Response:
    return await _handle(request, _resume)


@router.post("/controls/cap", response_model=None)
async def change_cap(request: Request) -> Response:
    return await _handle(request, _set_cap)


# --- API pour le hook PreToolUse ----------------------------------------------------------------
@router.get("/api/v1/control", dependencies=[Depends(ingest.require_token)], response_model=None)
async def control_api() -> Response:
    headers = {"Cache-Control": "no-store"}
    try:
        state = await run_in_threadpool(load_state)
    except sqlite3.Error as exc:
        log.warning("lecture des contrôles impossible : %s", type(exc).__name__)
        return JSONResponse({"detail": "base illisible"}, status_code=503, headers=headers)
    return JSONResponse(state.model_dump(), headers=headers)
