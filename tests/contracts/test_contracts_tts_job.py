"""Contrat contracts/tts-job.schema.json v2.0 (#249) : une réplique = un texte et une voix."""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def _load(name: str) -> Any:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    data: dict[str, Any] = _load("tts-job.schema.json")
    Draft202012Validator.check_schema(data)
    return data


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def resultat(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator({"$ref": "#/$defs/TtsResult", "$defs": schema["$defs"]})


@pytest.fixture
def valid() -> dict[str, Any]:
    data: dict[str, Any] = _load("examples/tts-job.json")
    return data


def test_schema_versionne_strict_en_2_0(schema: dict[str, Any]) -> None:
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"]["const"] == "2.0"
    assert schema["required"] == ["schema_version", "output_dir", "repliques"]
    assert schema["additionalProperties"] is False
    for name in ("Replique", "TtsResult", "RepliqueResult"):
        assert schema["$defs"][name]["additionalProperties"] is False


def test_exemple_valide_alterne_deux_voix(
    validator: Draft202012Validator, valid: dict[str, Any]
) -> None:
    assert list(validator.iter_errors(valid)) == []
    assert len({r["voix_id"] for r in valid["repliques"]}) == 2


def test_aucun_chemin_de_voix_de_reference_dans_le_contrat(schema: dict[str, Any]) -> None:
    cles = set(schema["$defs"]["Replique"]["properties"])
    assert cles == {"replique_id", "voix_id", "texte", "langue"}


def _job_1_0(d: dict[str, Any]) -> None:
    d["schema_version"] = "1.0"
    d["plans"] = [{"plan_id": "plan-1", "texte": "Bonjour.", "langue": "fr"}]
    del d["repliques"]


def _sans_repliques(d: dict[str, Any]) -> None:
    d["repliques"] = []


def _sans_voix(d: dict[str, Any]) -> None:
    del d["repliques"][0]["voix_id"]


def _voix_dangereuse(d: dict[str, Any]) -> None:
    d["repliques"][0]["voix_id"] = "../voix"


def _id_dangereux(d: dict[str, Any]) -> None:
    d["repliques"][0]["replique_id"] = "../x"


def _id_trop_long(d: dict[str, Any]) -> None:
    d["repliques"][0]["replique_id"] = "a" * 65


def _texte_vide(d: dict[str, Any]) -> None:
    d["repliques"][0]["texte"] = "\n \t"


def _texte_nul(d: dict[str, Any]) -> None:
    d["repliques"][0]["texte"] = "bon\u0000jour"


def _langue(d: dict[str, Any]) -> None:
    d["repliques"][0]["langue"] = "Darija"


def _chemin_de_reference(d: dict[str, Any]) -> None:
    d["repliques"][0]["reference"] = "/home/parent/voix.wav"


def _champ_racine(d: dict[str, Any]) -> None:
    d["voix"] = {}


MUTATIONS: list[Callable[[dict[str, Any]], None]] = [
    _job_1_0,
    _sans_repliques,
    _sans_voix,
    _voix_dangereuse,
    _id_dangereux,
    _id_trop_long,
    _texte_vide,
    _texte_nul,
    _langue,
    _chemin_de_reference,
    _champ_racine,
]


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda f: f.__name__)
def test_job_invalide_refuse(
    validator: Draft202012Validator, valid: dict[str, Any], mutate: Callable[..., None]
) -> None:
    job = copy.deepcopy(valid)
    mutate(job)
    assert not validator.is_valid(job)


def _sortie() -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "repliques": [
            {"replique_id": "r1", "wav_path": "/data/jobs/42/voix/r1.wav", "duree_s": 1.5},
            {"replique_id": "r2", "wav_path": "/data/jobs/42/voix/r2.wav", "duree_s": 0.8},
        ],
    }


def test_resultat_2_0_valide(resultat: Draft202012Validator) -> None:
    assert list(resultat.iter_errors(_sortie())) == []


@pytest.mark.parametrize(
    "champ", [("schema_version", "1.0"), ("plans", []), ("repliques", [])], ids=str
)
def test_resultat_hors_contrat_refuse(resultat: Draft202012Validator, champ: Any) -> None:
    cle, valeur = champ
    sortie = _sortie()
    sortie[cle] = valeur
    assert not resultat.is_valid(sortie)


def test_resultat_duree_nulle_ou_wav_absent_refuse(resultat: Draft202012Validator) -> None:
    sortie = _sortie()
    sortie["repliques"][0]["duree_s"] = 0
    assert not resultat.is_valid(sortie)
    sortie = _sortie()
    sortie["repliques"][1]["wav_path"] = "/data/r2.mp3"
    assert not resultat.is_valid(sortie)
