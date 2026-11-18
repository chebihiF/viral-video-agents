"""Point d'entrée FastAPI du dashboard (Sprint 0 : page vide + santé)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dashboard import db, tokens
from dashboard.ingest import router as ingest_router
from dashboard.otlp import router as otlp_router

VERSION = os.environ.get("APP_VERSION", "dev")
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
TEMPLATES.env.filters["thousands"] = tokens.thousands

app = FastAPI(title="Agents Dashboard", version=VERSION, docs_url=None, redoc_url=None)
app.include_router(ingest_router)
app.include_router(otlp_router)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "index.html", {"version": VERSION})


@app.get("/tokens", response_class=HTMLResponse)
def tokens_page(request: Request) -> HTMLResponse:
    with db.connect() as conn:
        report = tokens.totals(conn, datetime.now(UTC))
    context = {"version": VERSION, "report": report}
    return TEMPLATES.TemplateResponse(request, "tokens.html", context)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}
