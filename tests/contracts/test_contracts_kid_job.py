"""Contrat contracts/kid-job.schema.json : API jobs de l'appli enfant (#162)."""

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts" / "kid-job.schema.json").read_text("utf-8")
)
JOB_ID = "0b6f0c1e-6a43-4a8e-9a39-3c1f2d4e5a6b"
PROG = {"pct": 40, "restant_s": 90, "bientot": False}
MSG = "Je te génère la vidéo une fois à la maison."


def ok(defn: str, data: Any) -> bool:
    return Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{defn}"}).is_valid(data)


def test_schema_valide_draft_2020_12() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert all(d["additionalProperties"] is False for d in SCHEMA["$defs"].values())


@pytest.mark.parametrize(
    ("defn", "data", "valide"),
    [
        ("Submit", {"text": "Un chat qui vole"}, True),
        ("Submit", {"text": "a" * 500}, True),
        ("Submit", {"text": "a" * 501}, False),
        ("Submit", {"text": ""}, False),
        ("Submit", {"text": "  "}, False),
        ("Submit", {"text": "x", "autre": 1}, False),
        ("Accepted", {"job_id": JOB_ID}, True),
        ("Accepted", {"job_id": "../x"}, False),
        ("Deferred", {"status": "deferred", "message": MSG}, True),
        ("Deferred", {"status": "deferred", "message": "autre"}, False),
        ("Status", {"state": "running", "step": "images"}, True),
        ("Status", {"state": "fini", "step": "images"}, False),
        ("Status", {"state": "done", "step": "classification"}, False),
        ("Status", {"state": "done", "step": "montage", "text": "dictée"}, False),
        ("Status", {"state": "running", "step": "images", "progress": PROG}, True),
        ("Status", {"state": "done", "step": "montage", "progress": {**PROG, "pct": 100}}, True),
        (
            "Status",
            {"state": "running", "step": "images", "progress": {**PROG, "restant_s": None}},
            True,
        ),
        ("Status", {"state": "running", "step": "images", "progress": {**PROG, "pct": 101}}, False),
        ("Status", {"state": "running", "step": "images", "progress": {**PROG, "pct": -1}}, False),
        ("Status", {"state": "running", "step": "images", "progress": {**PROG, "pct": 1.5}}, False),
        (
            "Status",
            {"state": "running", "step": "images", "progress": {**PROG, "restant_s": -1}},
            False,
        ),
        (
            "Status",
            {"state": "running", "step": "images", "progress": {**PROG, "bientot": 1}},
            False,
        ),
        ("Status", {"state": "running", "step": "images", "progress": {"pct": 5}}, False),
        (
            "Status",
            {"state": "running", "step": "images", "progress": {**PROG, "text": "x"}},
            False,
        ),
        ("Progress", PROG, True),
        ("Status", {"state": "error", "step": "storyboard", "blocked": True}, True),
        ("Status", {"state": "error", "step": "storyboard", "blocked": False}, False),
        ("Status", {"state": "queued", "step": "storyboard", "waking": True}, True),
        ("Status", {"state": "queued", "step": "storyboard", "waking": False}, False),
    ],
)
def test_exemples(defn: str, data: Any, valide: bool) -> None:
    assert ok(defn, data) is valide
