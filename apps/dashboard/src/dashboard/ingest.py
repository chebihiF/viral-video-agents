"""POST /api/v1/events : ingestion authentifiée (contrat : contracts/ingest-api.md)."""

from __future__ import annotations

import hmac
import os
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from dashboard import db

MAX_BATCH = 100
MAX_BODY_BYTES = 256 * 1024

Id = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
OptionalName = Annotated[str | None, Field(max_length=128, strict=True)]


class AgentEvent(BaseModel):
    """Miroir de contracts/agent-event.schema.json (alignement vérifié par test)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: Id
    ts: Annotated[AwareDatetime, Field(strict=True)]
    hook_event_name: Annotated[str, Field(min_length=1, max_length=64, strict=True)]
    session_id: Id
    agent_type: OptionalName = None
    agent_id: OptionalName = None
    tool_name: OptionalName = None
    ticket: Annotated[int | None, Field(ge=1, strict=True)] = None

    def row(self) -> db.EventRow:
        values = self.model_dump()
        values["ts"] = self.ts.astimezone(UTC).isoformat()
        return tuple(values[col] for col in db.EVENT_COLUMNS)


Batch = Annotated[list[AgentEvent], Field(min_length=1, max_length=MAX_BATCH)]
_PAYLOAD: TypeAdapter[AgentEvent | list[AgentEvent]] = TypeAdapter(AgentEvent | Batch)

router = APIRouter(prefix="/api/v1")


def require_token(request: Request) -> None:
    """Fermé si INGEST_TOKEN absent ; comparaison en temps constant. Avant tout lecture du corps."""
    expected = os.environ.get("INGEST_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="ingestion non configurée")
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    valid = hmac.compare_digest(given.encode(), expected.encode())
    if scheme != "Bearer" or not valid:
        raise HTTPException(
            status_code=401, detail="jeton invalide", headers={"WWW-Authenticate": "Bearer"}
        )


def _too_large() -> HTTPException:
    return HTTPException(status_code=413, detail=f"corps limité à {MAX_BODY_BYTES} octets")


async def read_body(request: Request) -> bytes:
    """Lecture bornée : refuse sur Content-Length puis sur le flux réel."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _too_large()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise _too_large()
    return bytes(body)


def parse_events(raw: bytes) -> list[AgentEvent]:
    try:
        payload = _PAYLOAD.validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        raise RequestValidationError(errors) from None
    return payload if isinstance(payload, list) else [payload]


def store_events(events: list[AgentEvent]) -> int:
    with db.connect() as conn:
        return db.insert_events(conn, [e.row() for e in events])


@router.post("/events", dependencies=[Depends(require_token)])
async def ingest_events(request: Request) -> JSONResponse:
    events = parse_events(await read_body(request))
    inserted = await run_in_threadpool(store_events, events)
    status = 202 if inserted else 200
    content = {"inserted": inserted, "duplicates": len(events) - inserted}
    return JSONResponse(status_code=status, content=content)
