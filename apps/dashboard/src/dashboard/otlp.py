"""POST /v1/metrics : tokens Claude Code en OTLP/HTTP JSON (contrat : contracts/token-usage.md)."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from dashboard import db
from dashboard.ingest import Id, read_body, require_token

TOKEN_METRIC = "claude_code.token.usage"  # noqa: S105 - nom de métrique, pas un secret
TokenType = Literal["input", "output", "cacheRead", "cacheCreation"]
TOKEN_TYPES: tuple[str, ...] = get_args(TokenType)
# Liste blanche stricte : attribut OTLP -> colonne. Tout le reste est ignoré.
ATTRIBUTES = {
    "session.id": "session_id",
    "agent.name": "agent_id",
    "model": "model",
    "type": "type",
}
DELTA = 1
NS = 10**9
MAX_COUNT = 10**12  # borne haute par point : protège SUM() SQLite (int64)
MAX_FUTURE_NS = 3600 * NS  # tolérance d'horloge : ts > maintenant + 1 h rejeté
REJECTED = (
    "points rejetés : attribut manquant ou invalide, valeur invalide ou temporalité non delta"
)


class _Otlp(BaseModel):
    """Enveloppe OTLP JSON : champs en lowerCamelCase, champs inconnus ignorés."""

    model_config = ConfigDict(alias_generator=to_camel, extra="ignore", frozen=True)


class AnyValue(_Otlp):
    string_value: str | None = None


class KeyValue(_Otlp):
    key: str
    value: AnyValue = AnyValue()


class DataPoint(_Otlp):
    attributes: list[KeyValue] = []
    start_time_unix_nano: int | str = 0
    time_unix_nano: int | str = 0
    as_int: int | str | None = None
    as_double: float | None = None


class Sum(_Otlp):
    aggregation_temporality: int = 0
    data_points: list[DataPoint] = []


class Metric(_Otlp):
    name: str = ""
    sum_: Sum | None = Field(default=None, alias="sum")


class ScopeMetrics(_Otlp):
    metrics: list[Metric] = []


class Resource(_Otlp):
    attributes: list[KeyValue] = []


class ResourceMetrics(_Otlp):
    resource: Resource = Resource()
    scope_metrics: list[ScopeMetrics] = []


class ExportRequest(_Otlp):
    resource_metrics: list[ResourceMetrics] = []


class TokenPoint(BaseModel):
    """Point de tokens retenu, après liste blanche (une ligne de `token_usage`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: Id
    agent_id: Id | None = None
    model: Id
    type: TokenType
    count: Annotated[int, Field(ge=0, le=MAX_COUNT)]
    start_ns: Annotated[int, Field(ge=0, lt=2**63)]
    time_ns: Annotated[int, Field(gt=0, lt=2**63)]

    def point_id(self) -> str:
        """Clé d'idempotence : série + intervalle (cf. contrat)."""
        key = [self.session_id, self.agent_id or "", self.model, self.type]
        raw = json.dumps([*key, self.start_ns, self.time_ns]).encode()
        return hashlib.sha256(raw).hexdigest()

    def row(self) -> db.TokenRow:
        seconds, nanos = divmod(self.time_ns, NS)
        ts = datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=nanos // 1000)
        iso = ts.isoformat(timespec="microseconds")
        return (
            self.point_id(),
            iso,
            self.session_id,
            self.agent_id,
            self.model,
            self.type,
            self.count,
        )


def _whitelisted(attributes: list[KeyValue]) -> dict[str, str]:
    return {
        ATTRIBUTES[kv.key]: kv.value.string_value
        for kv in attributes
        if kv.key in ATTRIBUTES and kv.value.string_value is not None
    }


def _count(dp: DataPoint) -> int | str | None:
    if dp.as_int is not None:
        return dp.as_int
    if dp.as_double is not None and math.isfinite(dp.as_double):
        return round(dp.as_double)
    return None


def _parse_point(dp: DataPoint, base: dict[str, str], now_ns: int) -> TokenPoint | None:
    candidate = {
        **base,
        **_whitelisted(dp.attributes),
        "count": _count(dp),
        "start_ns": dp.start_time_unix_nano,
        "time_ns": dp.time_unix_nano,
    }
    try:
        point = TokenPoint.model_validate(candidate)
    except ValidationError:
        return None
    return None if point.time_ns > now_ns + MAX_FUTURE_NS else point


def _token_sums(rm: ResourceMetrics) -> Iterator[Sum]:
    for scope in rm.scope_metrics:
        for metric in scope.metrics:
            if metric.name == TOKEN_METRIC and metric.sum_ is not None:
                yield metric.sum_


def token_points(request: ExportRequest, now_ns: int) -> tuple[list[TokenPoint], int]:
    """Extrait les points de tokens valides ; renvoie (retenus, nombre rejeté)."""
    accepted: list[TokenPoint] = []
    rejected = 0
    for rm in request.resource_metrics:
        base = _whitelisted(rm.resource.attributes)
        for sum_ in _token_sums(rm):
            if sum_.aggregation_temporality != DELTA:
                rejected += len(sum_.data_points)
                continue
            points = [_parse_point(dp, base, now_ns) for dp in sum_.data_points]
            accepted.extend(p for p in points if p is not None)
            rejected += points.count(None)
    return accepted, rejected


def parse_request(raw: bytes) -> ExportRequest:
    try:
        return ExportRequest.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        raise HTTPException(status_code=400, detail=errors) from None


def require_json(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="OTLP/HTTP JSON uniquement")


def store_points(points: list[TokenPoint]) -> int:
    with db.connect() as conn:
        return db.insert_token_usage(conn, [p.row() for p in points])


router = APIRouter()


@router.post("/v1/metrics", dependencies=[Depends(require_token), Depends(require_json)])
async def export_metrics(request: Request) -> JSONResponse:
    points, rejected = token_points(parse_request(await read_body(request)), time.time_ns())
    await run_in_threadpool(store_points, points)
    if not rejected:
        return JSONResponse(content={})
    partial = {"rejectedDataPoints": rejected, "errorMessage": REJECTED}
    return JSONResponse(content={"partialSuccess": partial})
