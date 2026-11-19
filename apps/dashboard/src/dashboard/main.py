"""Point d'entrée FastAPI du dashboard (routes : contracts/dashboard-routes.md)."""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import auth, db, quota, quota_view, tokens, trend
from dashboard.activity import router as activity_router
from dashboard.auth import router as auth_router
from dashboard.controls import router as controls_router
from dashboard.home import router as home_router
from dashboard.ingest import router as ingest_router
from dashboard.interactions import router as interactions_router
from dashboard.otlp import router as otlp_router
from dashboard.progress import router as progress_router
from dashboard.stream import router as stream_router

log = logging.getLogger(__name__)
VERSION = os.environ.get("APP_VERSION", "dev")
HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=HERE / "templates")
TEMPLATES.env.filters["thousands"] = tokens.thousands

app = FastAPI(title="Agents Dashboard", version=VERSION, docs_url=None, redoc_url=None)
app.include_router(ingest_router)
app.include_router(otlp_router)
app.include_router(auth_router)
app.include_router(home_router)
app.include_router(activity_router)
app.include_router(interactions_router)
app.include_router(stream_router)
app.include_router(progress_router)
app.include_router(controls_router)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/tokens", response_class=HTMLResponse)
def tokens_page(request: Request) -> Response:
    if not auth.is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    try:
        context = _tokens_context()
    except sqlite3.Error as exc:
        log.warning("lecture des tokens impossible : %s", type(exc).__name__)
        context = {"version": VERSION, "report": None}
        return TEMPLATES.TemplateResponse(request, "tokens.html", context, status_code=503)
    return TEMPLATES.TemplateResponse(request, "tokens.html", context)


def _tokens_context() -> dict[str, object]:
    with db.connect() as conn:
        now = datetime.now(UTC)
        report = tokens.totals(conn, now)
        cap = db.read_controls(conn)[2]
        return {
            "version": VERSION,
            "report": report,
            "quota": quota_view.build(conn, now, quota.read_quota()),
            "days": trend.daily_usage(conn, now),
            "cap_text": trend.cap_text(cap, sum(t.last_24h for t in report.by_model)),
        }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}
