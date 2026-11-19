"""TTS Piper (#51) : faux binaire Piper (WAV silencieux de durée connue), aucun texte loggé."""

import json
import logging
import stat
import sys
import wave
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from media_service.tts import (
    SCHEMA_VERSION,
    TtsError,
    parse_job,
    synthetiser,
    verifier_configuration,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
PRENOM = "Secret-Prenom-Enfant"  # noqa: S105 - marqueur de fuite

# Faux Piper : lit le texte sur stdin, écrit un WAV silencieux de 0,25 s par mot.
FAKE_PIPER = f"""#!{sys.executable}
import sys, wave
args = sys.argv[1:]
model = args[args.index("--model") + 1]
out = args[args.index("--output_file") + 1]
text = sys.stdin.read()
rate = 24000
with wave.open(out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(b"\\0\\0" * (rate // 4) * len(text.split()))
"""


def _schema() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((CONTRACTS / "tts-job.schema.json").read_text("utf-8"))
    return data


def _plans() -> list[dict[str, str]]:
    return [
        {"plan_id": "plan-1", "texte": "un deux trois quatre", "langue": "fr"},
        {"plan_id": "plan-2", "texte": f"{PRENOM} dit bonjour", "langue": "fr"},
    ]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    binary = tmp_path / "piper"
    binary.write_text(FAKE_PIPER, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    model = tmp_path / "voix.onnx"
    model.write_bytes(b"model")
    return {"PIPER_BIN": str(binary), "PIPER_MODEL": str(model)}


@pytest.fixture
def out(tmp_path: Path) -> Path:
    path = tmp_path / "voix"
    path.mkdir()
    return path


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def test_voix_par_plan(env: dict[str, str], out: Path) -> None:
    results = synthetiser(_plans(), out, env=env)
    assert [r.plan_id for r in results] == ["plan-1", "plan-2"]
    assert [r.wav_path for r in results] == [out / "plan-1.wav", out / "plan-2.wav"]
    for r in results:
        assert r.wav_path.is_file()
        assert r.duree_s == pytest.approx(_wav_seconds(r.wav_path))
    assert [r.duree_s for r in results] == [pytest.approx(1.0), pytest.approx(0.75)]
    assert sorted(p.name for p in out.iterdir()) == ["plan-1.wav", "plan-2.wav"]


def test_texte_vide_nomme_le_plan_sans_logger_le_texte(
    env: dict[str, str], out: Path, caplog: pytest.LogCaptureFixture
) -> None:
    plans = _plans()
    plans[0]["texte"] = f"{PRENOM} dit bonjour"
    plans[1]["texte"] = " \n "
    with caplog.at_level(logging.DEBUG), pytest.raises(TtsError, match="plan-2") as err:
        synthetiser(plans, out, env=env)
    assert PRENOM not in str(err.value) and PRENOM not in caplog.text
    assert list(out.iterdir()) == []  # validation avant toute synthèse


def test_verifier_configuration_nominal(env: dict[str, str]) -> None:
    binary, model = verifier_configuration(env)
    assert binary == env["PIPER_BIN"] and Path(binary).is_absolute()
    assert model == env["PIPER_MODEL"] and Path(model).is_absolute()


def test_verifier_configuration_erreurs_inchangees(env: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(TtsError, match="PIPER_BIN non configuré"):
        verifier_configuration({"PIPER_MODEL": env["PIPER_MODEL"]})
    with pytest.raises(TtsError, match="PIPER_BIN introuvable"):
        verifier_configuration({**env, "PIPER_BIN": str(tmp_path / "absent")})
    with pytest.raises(TtsError, match="PIPER_MODEL non configuré"):
        verifier_configuration({"PIPER_BIN": env["PIPER_BIN"]})
    with pytest.raises(TtsError, match="PIPER_MODEL introuvable"):
        verifier_configuration({**env, "PIPER_MODEL": str(tmp_path / "absent.onnx")})


def test_piper_bin_non_configure(env: dict[str, str], out: Path) -> None:
    del env["PIPER_BIN"]
    with pytest.raises(TtsError, match="PIPER_BIN"):
        synthetiser(_plans(), out, env=env)


def test_piper_bin_introuvable(env: dict[str, str], out: Path, tmp_path: Path) -> None:
    env["PIPER_BIN"] = str(tmp_path / "absent")
    with pytest.raises(TtsError, match="PIPER_BIN"):
        synthetiser(_plans(), out, env=env)


def test_piper_model_manquant(env: dict[str, str], out: Path, tmp_path: Path) -> None:
    env["PIPER_MODEL"] = str(tmp_path / "absent.onnx")
    with pytest.raises(TtsError, match="PIPER_MODEL"):
        synthetiser(_plans(), out, env=env)
    del env["PIPER_MODEL"]
    with pytest.raises(TtsError, match="PIPER_MODEL"):
        synthetiser(_plans(), out, env=env)


def test_echec_au_2e_plan_ne_laisse_aucun_fichier(
    env: dict[str, str], out: Path, tmp_path: Path
) -> None:
    flaky = tmp_path / "piper-flaky"
    counter = tmp_path / "calls"
    body = (
        "import sys, wave, pathlib\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        "text = sys.stdin.read()\n"
        "if n >= 1:\n"
        "    sys.stderr.write(text)\n"
        "    sys.exit(3)\n"
        "out = sys.argv[sys.argv.index('--output_file') + 1]\n"
        "w = wave.open(out, 'wb')\n"
        "w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)\n"
        "w.writeframes(b'\\0\\0' * 8000); w.close()\n"
    )
    flaky.write_text(f"#!{sys.executable}\n{body}")
    flaky.chmod(0o755)
    env["PIPER_BIN"] = str(flaky)
    plans = _plans()
    plans[1]["texte"] = f"{PRENOM} dit bonjour"
    with pytest.raises(TtsError, match="plan-2") as err:
        synthetiser(plans, out, env=env)
    assert counter.read_text() == "2"  # le 1er plan avait réussi
    assert PRENOM not in str(err.value)
    assert list(out.iterdir()) == []  # tout ou rien


def test_synthetiser_accepte_les_plan_de_parse_job(env: dict[str, str], out: Path) -> None:
    job = parse_job({"schema_version": "1.0", "output_dir": str(out), "plans": _plans()})
    results = synthetiser(job.plans, job.output_dir, env=env)
    assert [r.plan_id for r in results] == ["plan-1", "plan-2"]


def test_erreur_systeme_a_l_ecriture_nomme_le_plan(
    env: dict[str, str], out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any) -> None:
        raise PermissionError("refusé")

    monkeypatch.setattr("media_service.tts.os.replace", boom)
    with pytest.raises(TtsError, match="plan-1"):
        synthetiser(_plans(), out, env=env)
    assert list(out.iterdir()) == []


def test_sortie_sans_wav_valide(env: dict[str, str], out: Path, tmp_path: Path) -> None:
    lazy = tmp_path / "piper-lazy"
    lazy.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n")
    lazy.chmod(0o755)
    env["PIPER_BIN"] = str(lazy)
    with pytest.raises(TtsError, match="plan-1"):
        synthetiser(_plans(), out, env=env)
    assert list(out.iterdir()) == []


def test_plan_id_en_double_ou_dangereux(env: dict[str, str], out: Path) -> None:
    plans = _plans()
    plans[1]["plan_id"] = "plan-1"
    with pytest.raises(TtsError, match="plan-1"):
        synthetiser(plans, out, env=env)
    plans[1]["plan_id"] = "../evil"
    with pytest.raises(TtsError, match="plan_id"):
        synthetiser(plans, out, env=env)


def test_dossier_de_sortie_absent(env: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(TtsError, match="dossier"):
        synthetiser(_plans(), tmp_path / "absent", env=env)


def test_aucun_texte_dans_les_logs(
    env: dict[str, str], out: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        synthetiser(_plans(), out, env=env)
    assert PRENOM not in caplog.text


# --- contrat -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture
def valid() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((CONTRACTS / "examples/tts-job.json").read_text("utf-8"))
    return data


def test_schema_versionne_et_strict() -> None:
    schema = _schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "schema_version" in schema["required"]
    assert schema["additionalProperties"] is False
    for name in ("Plan", "TtsResult", "PlanResult"):
        assert schema["$defs"][name]["additionalProperties"] is False


def test_exemple_valide_schema_et_parseur(
    validator: Draft202012Validator, valid: dict[str, Any]
) -> None:
    assert SCHEMA_VERSION == "1.0"
    assert list(validator.iter_errors(valid)) == []
    job = parse_job(valid)
    assert [p.plan_id for p in job.plans] == ["plan-1", "plan-2"]
    assert job.output_dir == Path("/data/jobs/42/voix")


def test_sortie_conforme_au_schema(env: dict[str, str], out: Path) -> None:
    results = synthetiser(_plans(), out, env=env)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "plans": [
            {"plan_id": r.plan_id, "wav_path": str(r.wav_path), "duree_s": r.duree_s}
            for r in results
        ],
    }
    result_schema = {**_schema(), "$ref": "#/$defs/TtsResult"}
    for key in ("required", "properties", "additionalProperties", "type"):
        del result_schema[key]
    Draft202012Validator(result_schema).validate(payload)


def _no_plans(d: dict[str, Any]) -> None:
    d["plans"] = []


def _no_texte(d: dict[str, Any]) -> None:
    del d["plans"][0]["texte"]


def _blank_texte(d: dict[str, Any]) -> None:
    d["plans"][0]["texte"] = "\n \t"


def _nul_texte(d: dict[str, Any]) -> None:
    d["plans"][0]["texte"] = "bon\u0000jour"


def _bad_plan_id(d: dict[str, Any]) -> None:
    d["plans"][0]["plan_id"] = "../x"


def _bad_langue(d: dict[str, Any]) -> None:
    d["plans"][0]["langue"] = "Français"


def _extra_root(d: dict[str, Any]) -> None:
    d["extra"] = 1


def _extra_plan(d: dict[str, Any]) -> None:
    d["plans"][0]["extra"] = 1


def _bad_version(d: dict[str, Any]) -> None:
    d["schema_version"] = "2.0"


def _no_output_dir(d: dict[str, Any]) -> None:
    del d["output_dir"]


MUTATIONS = [
    _no_plans,
    _no_texte,
    _blank_texte,
    _nul_texte,
    _bad_plan_id,
    _bad_langue,
    _extra_root,
    _extra_plan,
    _bad_version,
    _no_output_dir,
]


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda f: f.__name__)
def test_job_invalide_refuse_par_schema_et_parseur(
    validator: Draft202012Validator, valid: dict[str, Any], mutate: Any
) -> None:
    mutate(valid)
    assert not validator.is_valid(valid)
    with pytest.raises(TtsError):
        parse_job(valid)
