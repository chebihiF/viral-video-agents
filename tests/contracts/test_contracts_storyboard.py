"""Contrat contracts/storyboard.schema.json : sortie du conteur, entrée du pipeline (#31)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def _load(name: str) -> Any:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = _load("storyboard.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture
def valid() -> dict[str, Any]:
    data: dict[str, Any] = _load("examples/storyboard.valid.json")
    return data


def test_schema_is_versioned_draft_2020_12() -> None:
    schema = _load("storyboard.schema.json")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "schema_version" in schema["required"]
    assert schema["additionalProperties"] is False


def test_valid_example_passes(validator: Draft202012Validator, valid: dict[str, Any]) -> None:
    assert list(validator.iter_errors(valid)) == []


def test_invalid_example_fails(validator: Draft202012Validator) -> None:
    assert not validator.is_valid(_load("examples/storyboard.invalid.json"))


def test_plan_without_narration_fails_naming_the_field(
    validator: Draft202012Validator, valid: dict[str, Any]
) -> None:
    del valid["plans"][0]["narration_fr"]
    errors = list(validator.iter_errors(valid))
    assert errors
    assert any("narration_fr" in e.message for e in errors)


@pytest.mark.parametrize("count", [1, 5])
def test_plan_count_out_of_range_is_refused(
    validator: Draft202012Validator, valid: dict[str, Any], count: int
) -> None:
    plan = valid["plans"][0]
    valid["plans"] = [copy.deepcopy(plan) for _ in range(count)]
    assert not validator.is_valid(valid)


def test_classification_is_closed(validator: Draft202012Validator, valid: dict[str, Any]) -> None:
    valid["classification"] = "inconnu"
    assert not validator.is_valid(valid)


def test_unknown_property_is_refused(
    validator: Draft202012Validator, valid: dict[str, Any]
) -> None:
    valid["plans"][0]["extra"] = "x"
    assert not validator.is_valid(valid)


def test_duree_defaults_to_3_seconds() -> None:
    schema = _load("storyboard.schema.json")
    assert schema["$defs"]["Plan"]["properties"]["duree_s"]["default"] == 3
