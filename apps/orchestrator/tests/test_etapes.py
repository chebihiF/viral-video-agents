"""Étapes réelles assemblées (#150) : faux Ollama et faux Piper, aucun réseau, aucun GPU."""

from __future__ import annotations

import json
import logging
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from orchestrator.etapes import etapes_reelles
from orchestrator.pipeline import Etapes, lancer
from orchestrator.steps_llm import ErreurConfiguration

RACINE = Path(__file__).resolve().parents[3]
CONTRACTS = RACINE / "contracts"
DICTEE = "Dictée factice : le hibou violet secret joue du tambour."
URL = "http://ollama.test:11434"
LIBRE = json.loads((CONTRACTS / "examples" / "classification.libre.json").read_text("utf-8"))
STORYBOARD = json.loads((CONTRACTS / "examples" / "storyboard.valid.json").read_text("utf-8"))
RUN = Draft202012Validator(json.loads((CONTRACTS / "pipeline-run.schema.json").read_text("utf-8")))

FAKE_PIPER = f"""#!{sys.executable}
import sys, wave
out = sys.argv[sys.argv.index("--output_file") + 1]
text = sys.stdin.read()
with wave.open(out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(24000)
    w.writeframes(b"\\0\\0" * 6000 * max(1, len(text.split())))
"""


class FauxOllama:
    """Transport factice : une classification puis un storyboard ; aucune socket."""

    def __init__(self) -> None:
        self.requetes: list[dict[str, Any]] = []

    def __call__(self, url: str, corps: bytes, timeout: float) -> bytes:
        requete = json.loads(corps)
        self.requetes.append(requete)
        contenu = LIBRE if len(self.requetes) == 1 else STORYBOARD
        return json.dumps(
            {"message": {"role": "assistant", "content": json.dumps(contenu)}}
        ).encode()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    binaire = tmp_path / "piper"
    binaire.write_text(FAKE_PIPER, encoding="utf-8")
    binaire.chmod(binaire.stat().st_mode | stat.S_IXUSR)
    modele = tmp_path / "voix.onnx"
    modele.write_bytes(b"model")
    return {"OLLAMA_URL": URL, "PIPER_BIN": str(binaire), "PIPER_MODEL": str(modele)}


# Scénario: étapes injectées


def test_cinq_etapes_presentes_et_classification_par_ollama(env: dict[str, str]) -> None:
    faux = FauxOllama()
    etapes = etapes_reelles(env, transport=faux)
    assert isinstance(etapes, Etapes)
    assert all(callable(e) for e in (etapes.classifier, etapes.storyboard, etapes.voix))
    assert all(callable(e) for e in (etapes.image, etapes.clip, etapes.montage))
    assert etapes.classifier(DICTEE) == LIBRE
    assert len(faux.requetes) == 1
    assert faux.requetes[0]["messages"][1]["content"].startswith("<dictee>")


# Scénarios: configuration absente (échec fermé, aucun appel réseau)


@pytest.mark.parametrize("variable", ["OLLAMA_URL", "PIPER_BIN", "PIPER_MODEL"])
def test_configuration_absente_leve_avant_tout_appel(env: dict[str, str], variable: str) -> None:
    del env[variable]
    faux = FauxOllama()
    with pytest.raises(ErreurConfiguration, match=variable):
        etapes_reelles(env, transport=faux)
    assert faux.requetes == []


# Scénario: image/clip non branchés


def test_image_non_branchee_ferme_le_run_sans_mp4(
    env: dict[str, str], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    faux = FauxOllama()
    etapes = etapes_reelles(env, transport=faux)
    with caplog.at_level(logging.DEBUG):
        run = lancer(DICTEE, etapes, run_id="r150", workdir=str(tmp_path / "jobs"))
    RUN.validate(run)
    assert run["etat"] == "erreur"
    assert [(e["nom"], e["statut"]) for e in run["etapes"]] == [
        ("classification", "ok"),
        ("storyboard", "ok"),
        ("voix", "ok"),
        ("image", "erreur"),
    ]
    assert "video_path" not in run
    assert list(tmp_path.rglob("*.mp4")) == []
    for texte in (json.dumps(run), caplog.text):
        assert "hibou" not in texte
        assert "secret" not in texte


def test_image_et_clip_sont_des_points_d_injection(env: dict[str, str]) -> None:
    etapes = etapes_reelles(
        env,
        transport=FauxOllama(),
        image=lambda job_id, prompt: f"{job_id}.png",
        clip=lambda job: "clip.mp4",
    )
    assert etapes.image("j", "p") == "j.png"
    assert etapes.clip({}) == "clip.mp4"


def test_clip_non_branche_leve_erreur_de_configuration(env: dict[str, str]) -> None:
    etapes = etapes_reelles(env, transport=FauxOllama())
    with pytest.raises(ErreurConfiguration, match="gpu-worker"):
        etapes.clip({})
    with pytest.raises(ErreurConfiguration, match="gpu-worker"):
        etapes.image("j", "p")
