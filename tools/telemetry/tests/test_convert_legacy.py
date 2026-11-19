"""Convertisseur ponctuel events.jsonl ancien format -> agent-event (#166).

Échantillons synthétiques uniquement ; ingestion factice locale, jamais le VPS.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

import convert_legacy
import ship
from test_ship import FakeIngest

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "agent-event.schema.json").read_text(
        encoding="utf-8"
    )
)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
TOKEN = "jeton-factice-de-test"  # noqa: S105 - jeton factice de test


def legacy(i: int = 0, **extra: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-1",
        "tool_name": "Bash",
        "ts": 1794972191.9033191 + i,
    }
    line.update(extra)
    return line


def write(path: Path, lines: list[Any]) -> Path:
    path.write_text(
        "".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def server() -> Iterator[FakeIngest]:
    fake = FakeIngest()
    yield fake
    fake.close()


def test_conversion_conforme_au_schema() -> None:
    event = convert_legacy.convert(legacy(agent_type="dev-backend", agent_id="a1"))
    assert event is not None
    assert list(VALIDATOR.iter_errors(event)) == []
    assert event["ts"].endswith("+00:00")
    assert datetime.fromisoformat(event["ts"]).timestamp() == pytest.approx(1794972191.9033191)
    assert event["hook_event_name"] == "PostToolUse"
    assert "ticket" not in event


def test_event_id_deterministe_et_independant_de_l_ordre_des_cles() -> None:
    a = convert_legacy.convert(legacy())
    b = convert_legacy.convert(dict(reversed(list(legacy().items()))))
    c = convert_legacy.convert(legacy(1))
    assert a is not None and b is not None and c is not None
    assert a["event_id"] == b["event_id"] != c["event_id"]
    assert len(a["event_id"]) == 64


def test_ticket_deduit_de_la_branche() -> None:
    event = convert_legacy.convert(legacy(branch="feat/82-xyz"))
    assert event is not None
    assert event["ticket"] == 82
    assert "branch" not in event and "feat/82-xyz" not in json.dumps(event)


@pytest.mark.parametrize("branch", ["main", "feature/82-x", "feat/x-82", "", 82])
def test_pas_de_ticket_hors_branche_de_ticket(branch: Any) -> None:
    event = convert_legacy.convert(legacy(branch=branch))
    assert event is not None
    assert "ticket" not in event


def test_aucun_texte_sensible_dans_l_evenement() -> None:
    secret: dict[str, Any] = {
        "prompt": "dictée de l'enfant : Lucas",
        "tool_input": {"command": "cat /Users/parent/secret"},
        "tool_response": "sortie confidentielle",
        "cwd": "/Users/parent/dev",
        "transcript_path": "/Users/parent/.claude/x.jsonl",
    }
    event = convert_legacy.convert(legacy(**secret))
    assert event is not None
    dumped = json.dumps(event)
    for fragment in ("Lucas", "secret", "confidentielle", "/Users/parent"):
        assert fragment not in dumped
    assert set(event) <= set(SCHEMA["properties"])


@pytest.mark.parametrize(
    "bad",
    [
        {"session_id": "s", "ts": 1.0},
        {"hook_event_name": "X", "ts": 1.0},
        {"hook_event_name": "X", "session_id": "s"},
        {"hook_event_name": "X", "session_id": "s", "ts": "hier"},
        {"hook_event_name": "X", "session_id": "s", "ts": True},
        {"hook_event_name": "X", "session_id": "s", "ts": 1e300},
        {"hook_event_name": "", "session_id": "s", "ts": 1.0},
        ["pas", "un", "objet"],
    ],
)
def test_ligne_inconvertible_ignoree(bad: Any) -> None:
    assert convert_legacy.convert(bad) is None


def test_champs_optionnels_invalides_ecartes_et_tronques() -> None:
    event = convert_legacy.convert(legacy(agent_type=12, tool_name="x" * 300))
    assert event is not None
    assert "agent_type" not in event
    assert len(event["tool_name"]) == 128


def test_dry_run_par_defaut_n_envoie_rien(
    tmp_path: Path, server: FakeIngest, capsys: pytest.CaptureFixture[str], monkeypatch: Any
) -> None:
    monkeypatch.setenv("VVA_INGEST_URL", server.url)
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    src = write(
        tmp_path / "events.jsonl",
        [legacy(0), legacy(1, branch="fix/7-a"), "{pas du json", {"ts": 1}, ""],
    )
    assert convert_legacy.main([str(src)]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "converties : 2" in out and "ignorées : 2" in out and "ticket déduit : 1" in out
    assert TOKEN not in out
    assert server.hits == 0


def test_deux_conversions_donnent_les_memes_ids(tmp_path: Path) -> None:
    src = write(tmp_path / "events.jsonl", [legacy(i) for i in range(5)])
    first = convert_legacy.convert_file(src)
    second = convert_legacy.convert_file(src)
    assert [e["event_id"] for e in first.events] == [e["event_id"] for e in second.events]
    assert len({e["event_id"] for e in first.events}) == 5


def test_envoi_par_lots_de_100_au_plus(
    tmp_path: Path, server: FakeIngest, monkeypatch: Any
) -> None:
    monkeypatch.setenv("VVA_INGEST_URL", server.url)
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    src = write(tmp_path / "events.jsonl", [legacy(i) for i in range(250)] + ["{"])
    assert convert_legacy.main([str(src), "--send"]) == 0
    assert [len(b) for b in server.batches] == [100, 100, 50]
    assert set(server.auth) == {f"Bearer {TOKEN}"}
    for batch in server.batches:
        assert all(list(VALIDATOR.iter_errors(e)) == [] for e in batch)


def test_deux_envois_ne_creent_aucun_doublon(
    tmp_path: Path, server: FakeIngest, monkeypatch: Any
) -> None:
    store: set[str] = set()

    def respond(batch: list[dict[str, Any]]) -> int:
        new = {e["event_id"] for e in batch} - store
        store.update(new)
        return 202 if new else 200

    server.respond = respond
    monkeypatch.setenv("VVA_INGEST_URL", server.url)
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    src = write(tmp_path / "events.jsonl", [legacy(i) for i in range(30)])
    assert convert_legacy.main([str(src), "--send"]) == 0
    assert convert_legacy.main([str(src), "--send"]) == 0
    assert len(store) == 30
    assert len(server.received_ids()) == 60


@pytest.mark.parametrize("missing", ["VVA_INGEST_URL", "VVA_INGEST_TOKEN"])
def test_send_sans_configuration_echoue_ferme(
    tmp_path: Path,
    server: FakeIngest,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: Any,
    missing: str,
) -> None:
    monkeypatch.setenv("VVA_INGEST_URL", server.url)
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    monkeypatch.delenv(missing)
    src = write(tmp_path / "events.jsonl", [legacy()])
    assert convert_legacy.main([str(src), "--send"]) == 2
    err = capsys.readouterr().err
    assert "VVA_INGEST_URL" in err and "VVA_INGEST_TOKEN" in err
    assert TOKEN not in err
    assert server.hits == 0


def test_send_refuse_une_url_http_distante(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: Any
) -> None:
    monkeypatch.setenv("VVA_INGEST_URL", "http://exemple.invalid/api/v1/events")
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    src = write(tmp_path / "events.jsonl", [legacy()])
    assert convert_legacy.main([str(src), "--send"]) == 2
    assert TOKEN not in capsys.readouterr().err


def test_serveur_en_erreur_code_retry_sans_fuite_du_jeton(
    tmp_path: Path,
    server: FakeIngest,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: Any,
) -> None:
    server.respond = lambda _b: 500
    monkeypatch.setenv("VVA_INGEST_URL", server.url)
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    src = write(tmp_path / "events.jsonl", [legacy()])
    assert convert_legacy.main([str(src), "--send"]) == ship.EXIT_RETRY
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_jeton_refuse_code_config(tmp_path: Path, server: FakeIngest, monkeypatch: Any) -> None:
    server.respond = lambda _b: 401
    monkeypatch.setenv("VVA_INGEST_URL", server.url)
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    src = write(tmp_path / "events.jsonl", [legacy()])
    assert convert_legacy.main([str(src), "--send"]) == ship.EXIT_CONFIG


def test_lot_refuse_renvoye_evenement_par_evenement(
    tmp_path: Path, server: FakeIngest, monkeypatch: Any
) -> None:
    bad_ts = convert_legacy.convert(legacy(5))
    assert bad_ts is not None
    bad_id = bad_ts["event_id"]
    server.respond = lambda batch: 422 if any(e["event_id"] == bad_id for e in batch) else 202
    monkeypatch.setenv("VVA_INGEST_URL", server.url)
    monkeypatch.setenv("VVA_INGEST_TOKEN", TOKEN)
    src = write(tmp_path / "events.jsonl", [legacy(i) for i in range(10)])
    assert convert_legacy.main([str(src), "--send"]) == 0
    assert bad_id not in server.received_ids()
    assert len(server.received_ids()) == 9


def test_fichier_absent_code_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert convert_legacy.main([str(tmp_path / "absent.jsonl")]) == 2
    assert "introuvable" in capsys.readouterr().err
