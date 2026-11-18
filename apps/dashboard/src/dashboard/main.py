"""Point d'entrée FastAPI du dashboard (Sprint 0 : page vide + santé)."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dashboard.ingest import router as ingest_router

VERSION = os.environ.get("APP_VERSION", "dev")
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")

app = FastAPI(title="Agents Dashboard", version=VERSION, docs_url=None, redoc_url=None)
app.include_router(ingest_router)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "index.html", {"version": VERSION})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}
