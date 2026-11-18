"""Point d'entrée FastAPI du dashboard (routes : contracts/dashboard-routes.md)."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard.activity import router as activity_router
from dashboard.auth import router as auth_router
from dashboard.ingest import router as ingest_router
from dashboard.interactions import router as interactions_router
from dashboard.stream import router as stream_router

VERSION = os.environ.get("APP_VERSION", "dev")
HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=HERE / "templates")

app = FastAPI(title="Agents Dashboard", version=VERSION, docs_url=None, redoc_url=None)
app.include_router(ingest_router)
app.include_router(auth_router)
app.include_router(activity_router)
app.include_router(interactions_router)
app.include_router(stream_router)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "index.html", {"version": VERSION})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}
