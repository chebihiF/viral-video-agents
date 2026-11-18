"""Réception OTLP/HTTP JSON des métriques de tokens (#7)."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dashboard import ingest, otlp
from dashboard.main import app

TOKEN = "test-token-not-a-secret"  # noqa: S105 - jeton factice de test
URL = "/v1/metrics"
CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "token-usage.md"
T0 = 1_763_460_000_000_000_000  # 2025-11-18T10:00:00Z en nanosecondes
INTERVAL = 60_000_000_000
NS = 10**9


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv("INGEST_TOKEN", TOKEN)
    return path


@pytest.fixture
def client(db_path: Path) -> TestClient:
    return TestClient(app)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def attr(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def point(
    type_: str = "input",
    value: Any = "1200",
    *,
    model: str = "claude-opus-4-1",
    agent: str | None = None,
    start: int = T0,
    extra: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    attributes = [
        attr("user.email", "parent@example.org"),
        attr("user.account_uuid", "acc-123"),
        attr("organization.id", "org-1"),
        attr("session.id", "sess-1"),
        attr("terminal.type", "iTerm.app"),
        attr("model", model),
        attr("type", type_),
        attr("query_source", "subagent" if agent else "main"),
        *([attr("agent.name", agent)] if agent else []),
        *(extra or []),
    ]
    key = "asDouble" if isinstance(value, float) else "asInt"
    return {
        "attributes": attributes,
        "startTimeUnixNano": str(start),
        "timeUnixNano": str(start + INTERVAL),
        key: value,
    }


def payload(points: list[dict[str, Any]], temporality: int = 1) -> dict[str, Any]:
    """Charge OTLP JSON telle qu'émise par Claude Code (forme ExportMetricsServiceRequest)."""
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        attr("service.name", "claude-code"),
                        attr("service.version", "2.1.327"),
                        attr("host.arch", "arm64"),
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "com.anthropic.claude_code", "version": "2.1.327"},
                        "metrics": [
                            {
                                "name": "claude_code.cost.usage",
                                "unit": "USD",
                                "sum": {
                                    "aggregationTemporality": temporality,
                                    "isMonotonic": True,
                                    "dataPoints": [{"asDouble": 0.12, "timeUnixNano": str(T0)}],
                                },
                            },
                            {
                                "name": "claude_code.token.usage",
                                "unit": "tokens",
                                "sum": {
                                    "aggregationTemporality": temporality,
                                    "isMonotonic": True,
                                    "dataPoints": points,
                                },
                            },
                        ],
                    }
                ],
            }
        ]
    }


def rows(path: Path) -> list[tuple[Any, ...]]:
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        if not conn.execute("SELECT name FROM sqlite_master WHERE name='token_usage'").fetchall():
            return []
        return conn.execute(
            "SELECT ts, session_id, agent_id, model, type, count FROM token_usage ORDER BY type"
        ).fetchall()


# Scénario : réception des métriques
def test_token_metrics_are_stored(client: TestClient, db_path: Path) -> None:
    points = [
        point("input", "1200"),
        point("output", 350),
        point("cacheRead", 9000.0),
        point("cacheCreation", "40"),
    ]
    resp = client.post(URL, json=payload(points), headers=auth())
    assert resp.status_code == 200
    assert resp.json() == {}
    ts = "2025-11-18T10:01:00.000000+00:00"
    assert rows(db_path) == [
        (ts, "sess-1", None, "claude-opus-4-1", "cacheCreation", 40),
        (ts, "sess-1", None, "claude-opus-4-1", "cacheRead", 9000),
        (ts, "sess-1", None, "claude-opus-4-1", "input", 1200),
        (ts, "sess-1", None, "claude-opus-4-1", "output", 350),
    ]


def test_agent_name_is_stored_as_agent_id(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=payload([point(agent="backend")]), headers=auth())
    assert resp.status_code == 200
    assert rows(db_path)[0][2] == "backend"


def test_whitelisted_attributes_may_come_from_resource(client: TestClient, db_path: Path) -> None:
    body = payload([point()])
    dp = body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][1]["sum"]["dataPoints"][0]
    dp["attributes"] = [a for a in dp["attributes"] if a["key"] != "session.id"]
    body["resourceMetrics"][0]["resource"]["attributes"].append(attr("session.id", "sess-r"))
    assert client.post(URL, json=body, headers=auth()).status_code == 200
    assert rows(db_path)[0][1] == "sess-r"


def test_other_metrics_are_ignored(client: TestClient, db_path: Path) -> None:
    assert client.post(URL, json=payload([]), headers=auth()).status_code == 200
    assert rows(db_path) == []


def test_content_type_with_charset_is_accepted(client: TestClient, db_path: Path) -> None:
    headers = {**auth(), "Content-Type": "application/json; charset=utf-8"}
    resp = client.post(URL, content=json.dumps(payload([point()])), headers=headers)
    assert resp.status_code == 200
    assert len(rows(db_path)) == 1


# Idempotence
def test_resent_batch_does_not_double_totals(client: TestClient, db_path: Path) -> None:
    body = payload([point("input"), point("output", "10")])
    assert client.post(URL, json=body, headers=auth()).status_code == 200
    assert client.post(URL, json=body, headers=auth()).status_code == 200
    assert len(rows(db_path)) == 2


def test_next_interval_is_a_new_point(client: TestClient, db_path: Path) -> None:
    client.post(URL, json=payload([point()]), headers=auth())
    client.post(URL, json=payload([point(start=T0 + INTERVAL)]), headers=auth())
    assert len(rows(db_path)) == 2


# Rejets partiels
@pytest.mark.parametrize(
    "bad",
    [
        point("reasoning"),
        point(model=""),
        point(value=-1),
        point(value="12.5"),
        {**point(), "attributes": [attr("model", "m"), attr("type", "input")]},
        {k: v for k, v in point().items() if k != "asInt"},
        {**point(), "timeUnixNano": "0"},
        {**point(), "attributes": [{"key": "session.id", "value": {"intValue": "3"}}]},
    ],
    ids=[
        "type",
        "model-vide",
        "negatif",
        "decimal",
        "sans-session",
        "sans-valeur",
        "ts-nul",
        "non-chaine",
    ],
)
def test_invalid_point_is_rejected_others_kept(
    client: TestClient, db_path: Path, bad: dict[str, Any]
) -> None:
    resp = client.post(URL, json=payload([point("output", "5"), bad]), headers=auth())
    assert resp.status_code == 200
    partial = resp.json()["partialSuccess"]
    assert partial["rejectedDataPoints"] == 1
    assert partial["errorMessage"]
    assert [r[4] for r in rows(db_path)] == ["output"]


def test_cumulative_points_are_rejected(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=payload([point(), point("output")], temporality=2), headers=auth())
    assert resp.status_code == 200
    assert resp.json()["partialSuccess"]["rejectedDataPoints"] == 2
    assert rows(db_path) == []


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[]",
        b'{"resourceMetrics": "x"}',
        b'{"resourceMetrics": [{"scopeMetrics": 1}]}',
    ],
    ids=["json", "tableau", "liste", "scope"],
)
def test_malformed_envelope_is_400(client: TestClient, db_path: Path, content: bytes) -> None:
    headers = {**auth(), "Content-Type": "application/json"}
    assert client.post(URL, content=content, headers=headers).status_code == 400
    assert rows(db_path) == []


def test_protobuf_is_415(client: TestClient, db_path: Path) -> None:
    headers = {**auth(), "Content-Type": "application/x-protobuf"}
    assert client.post(URL, content=b"\x0a\x00", headers=headers).status_code == 415


# Scénario : endpoint protégé
@pytest.mark.parametrize(
    "headers", [{}, auth("wrong"), {"Authorization": TOKEN}], ids=["absent", "faux", "sans-bearer"]
)
def test_missing_or_wrong_token_is_401(
    client: TestClient, db_path: Path, headers: dict[str, str]
) -> None:
    assert client.post(URL, json=payload([point()]), headers=headers).status_code == 401
    assert rows(db_path) == []


def test_auth_checked_before_content_type_and_size(client: TestClient) -> None:
    headers = {**auth("wrong"), "Content-Type": "application/x-protobuf"}
    body = b" " * (ingest.MAX_BODY_BYTES + 1)
    assert client.post(URL, content=body, headers=headers).status_code == 401


def test_missing_ingest_token_closes_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INGEST_TOKEN")
    assert client.post(URL, json=payload([point()]), headers=auth()).status_code == 503


def test_body_over_limit_is_413(client: TestClient, db_path: Path) -> None:
    headers = {**auth(), "Content-Type": "application/json"}
    body = b" " * (ingest.MAX_BODY_BYTES + 1)
    assert client.post(URL, content=body, headers=headers).status_code == 413


# Scénario : aucun contenu
def test_no_prompt_or_personal_attribute_is_stored(client: TestClient, db_path: Path) -> None:
    secret = [attr("prompt", "raconte une histoire"), attr("user.prompt", "texte libre")]
    body = payload([point(extra=secret)])
    assert client.post(URL, json=body, headers=auth()).status_code == 200
    dump = "\n".join(sqlite3.connect(db_path).iterdump())
    for leaked in ("raconte", "texte libre", "parent@example.org", "acc-123", "org-1", "iTerm"):
        assert leaked not in dump


# Contrat : liste blanche alignée sur contracts/token-usage.md
def test_whitelist_matches_contract() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    section = text.split("### Attributs retenus", 1)[1].split("\n#", 1)[0]
    table = dict(re.findall(r"^\| `([^`]+)` \| `([^`]+)` \|", section, flags=re.MULTILINE))
    assert table == otlp.ATTRIBUTES
    types = re.search(r"\| `type` \| `type` \| oui \| (.+) \|", section)
    assert types is not None
    assert set(re.findall(r"`([^`]+)`", types.group(1))) == set(otlp.TOKEN_TYPES)
    assert f"`{otlp.TOKEN_METRIC}`" in text


# Cas limites (QA)
def test_empty_envelopes_are_accepted(client: TestClient, db_path: Path) -> None:
    for body in ({}, {"resourceMetrics": []}, {"resourceMetrics": [{}]}):
        assert client.post(URL, json=body, headers=auth()).status_code == 200
    assert rows(db_path) == []


def test_arabic_darija_ids_roundtrip(client: TestClient, db_path: Path) -> None:
    body = payload([point(agent="وكيل_الواجهة", model="نموذج-１")])
    assert client.post(URL, json=body, headers=auth()).status_code == 200
    assert rows(db_path)[0][2:4] == ("وكيل_الواجهة", "نموذج-１")


def test_huge_count_does_not_crash(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=payload([point(value="99999999999999999999")]), headers=auth())
    assert resp.status_code in (200, 400)
    assert rows(db_path) == []


@pytest.mark.parametrize("value", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_non_finite_double_is_rejected(client: TestClient, db_path: Path, value: float) -> None:
    body = payload([point("output", "5")])
    dp = {**point(), "asDouble": str(value)}
    dp.pop("asInt")
    body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][1]["sum"]["dataPoints"].append(dp)
    resp = client.post(
        URL, content=json.dumps(body), headers={**auth(), "Content-Type": "application/json"}
    )
    assert resp.status_code in (200, 400)
    assert [r[4] for r in rows(db_path)] in ([], ["output"])


def test_concurrent_double_submit_is_idempotent(client: TestClient, db_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    body = payload([point("input"), point("output", "10")])
    with ThreadPoolExecutor(4) as pool:
        codes = list(
            pool.map(lambda _: client.post(URL, json=body, headers=auth()).status_code, range(4))
        )
    assert codes == [200] * 4
    assert len(rows(db_path)) == 2


def test_session_id_not_leaked_in_response(client: TestClient) -> None:
    resp = client.post(URL, json=payload([point("reasoning")]), headers=auth())
    assert "sess-1" not in resp.text and "parent@example.org" not in resp.text


# Revue : valeurs bornées
@pytest.mark.parametrize(
    "value", [str(2**63), 1e300, str(10**12 + 1)], ids=["2^63", "1e300", "1e12+1"]
)
def test_huge_count_is_rejected_and_page_still_works(
    client: TestClient, db_path: Path, value: Any
) -> None:
    resp = client.post(
        URL, json=payload([point("output", "5"), point(value=value)]), headers=auth()
    )
    assert resp.status_code == 200
    assert resp.json()["partialSuccess"]["rejectedDataPoints"] == 1
    assert [r[4] for r in rows(db_path)] == ["output"]
    assert client.get("/tokens").status_code == 200


def test_count_at_upper_bound_is_accepted(client: TestClient, db_path: Path) -> None:
    resp = client.post(URL, json=payload([point(value=str(10**12))]), headers=auth())
    assert resp.json() == {}
    assert rows(db_path)[0][5] == 10**12


def test_future_point_is_rejected(client: TestClient, db_path: Path) -> None:
    now_ns = time.time_ns()
    future = point(start=now_ns + 2 * 3600 * NS)
    near = point("output", "5", start=now_ns + 30 * 60 * NS - INTERVAL)
    resp = client.post(URL, json=payload([future, near]), headers=auth())
    assert resp.status_code == 200
    assert resp.json()["partialSuccess"]["rejectedDataPoints"] == 1
    assert [r[4] for r in rows(db_path)] == ["output"]


def test_received_at_has_same_format_as_ts(client: TestClient, db_path: Path) -> None:
    client.post(URL, json=payload([point()]), headers=auth())
    with sqlite3.connect(db_path) as conn:
        ts, received_at = conn.execute("SELECT ts, received_at FROM token_usage").fetchone()
    iso = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")
    assert iso.match(ts)
    assert iso.match(received_at)
