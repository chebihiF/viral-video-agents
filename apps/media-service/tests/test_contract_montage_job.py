"""Contrat contracts/montage-job.schema.json : entrée du media-service (#34)."""

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from media_service.montage import MontageError, parse_job

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _load(name: str) -> Any:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = _load("montage-job.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture
def valid() -> dict[str, Any]:
    data: dict[str, Any] = _load("examples/montage-job.valid.json")
    return data


def test_schema_is_versioned_and_strict() -> None:
    schema = _load("montage-job.schema.json")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "schema_version" in schema["required"]
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Plan"]["additionalProperties"] is False


def test_valid_example_passes_schema_and_parser(
    validator: Draft202012Validator, valid: dict[str, Any]
) -> None:
    assert list(validator.iter_errors(valid)) == []
    job = parse_job(valid)
    assert [p.narration_fr for p in job.plans][0].startswith("Le petit chat")
    assert job.output_path == Path("/data/jobs/42/final.mp4")


def _without_plans(d: dict[str, Any]) -> None:
    d["plans"] = []


def _without_narration(d: dict[str, Any]) -> None:
    del d["plans"][0]["narration_fr"]


def _extra_root(d: dict[str, Any]) -> None:
    d["extra"] = 1


def _extra_plan(d: dict[str, Any]) -> None:
    d["plans"][0]["extra"] = 1


def _bad_version(d: dict[str, Any]) -> None:
    d["schema_version"] = "2.0"


def _blank_narration(d: dict[str, Any]) -> None:
    d["plans"][0]["narration_fr"] = "   "


def _not_mp4(d: dict[str, Any]) -> None:
    d["output_path"] = "/data/final.mov"


def _nul_clip(d: dict[str, Any]) -> None:
    d["plans"][0]["clip_path"] = "a\u0000b.mp4"


def _clip_not_string(d: dict[str, Any]) -> None:
    d["plans"][0]["clip_path"] = 3


def _blank_multiline_narration(d: dict[str, Any]) -> None:
    d["plans"][0]["narration_fr"] = "\n \t\n"


def _nul_narration(d: dict[str, Any]) -> None:
    d["plans"][0]["narration_fr"] = "bon\u0000jour"


def _blank_audio(d: dict[str, Any]) -> None:
    d["plans"][0]["audio_path"] = "  "


def _blank_clip(d: dict[str, Any]) -> None:
    d["plans"][0]["clip_path"] = " \n"


MUTATIONS = [
    _blank_multiline_narration,
    _nul_narration,
    _blank_audio,
    _blank_clip,
    _without_plans,
    _without_narration,
    _extra_root,
    _extra_plan,
    _bad_version,
    _blank_narration,
    _not_mp4,
    _nul_clip,
    _clip_not_string,
]


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda f: f.__name__)
def test_invalid_job_is_refused_by_schema_and_parser(
    validator: Draft202012Validator, valid: dict[str, Any], mutate: Any
) -> None:
    mutate(valid)
    assert not validator.is_valid(valid)
    with pytest.raises(MontageError):
        parse_job(valid)
