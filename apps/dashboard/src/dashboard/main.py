"""Point d'entrée FastAPI du dashboard (routes : contracts/dashboard-routes.md)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import auth, db, tokens
from dashboard.activity import router as activity_router
from dashboard.auth import router as auth_router
from dashboard.controls import router as controls_router
from dashboard.ingest import router as ingest_router
from dashboard.interactions import router as interactions_router
from dashboard.otlp import router as otlp_router
from dashboard.progress import router as progress_router
from dashboard.stream import router as stream_router

VERSION = os.environ.get("APP_VERSION", "dev")
HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=HERE / "templates")
TEMPLATES.env.filters["thousands"] = tokens.thousands

app = FastAPI(title="Agents Dashboard", version=VERSION, docs_url=None, redoc_url=None)
app.include_router(ingest_router)
app.include_router(otlp_router)
app.include_router(auth_router)
app.include_router(activity_router)
app.include_router(interactions_router)
app.include_router(stream_router)
app.include_router(progress_router)
app.include_router(controls_router)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/", response_model=None)
def index(request: Request) -> Response:
    target = "/progress" if auth.is_authenticated(request) else "/login"
    return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})


@app.get("/tokens", response_class=HTMLResponse)
def tokens_page(request: Request) -> Response:
    if not auth.is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    with db.connect() as conn:
        report = tokens.totals(conn, datetime.now(UTC))
    context = {"version": VERSION, "report": report}
    return TEMPLATES.TemplateResponse(request, "tokens.html", context)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}
