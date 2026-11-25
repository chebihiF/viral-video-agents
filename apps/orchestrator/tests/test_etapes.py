"""Étapes réelles assemblées (#150, #245) : faux Ollama, Piper et ComfyUI ; aucun réseau ni GPU."""

from __future__ import annotations

import json
import logging
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from test_steps_gpu import FauxComfy, Horloge

from orchestrator.etapes import etapes_reelles
from orchestrator.pipeline import Etapes, lancer
from orchestrator.steps_gpu import ClientComfy
from orchestrator.steps_llm import ErreurConfiguration

RACINE = Path(__file__).resolve().parents[3]
CONTRACTS = RACINE / "contracts"
DICTEE = "Dictée factice : le hibou violet secret joue du tambour."
URL = "http://ollama.test:11434"
COMFY = "http://127.0.0.1:8188"
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
        if url.endswith("/api/generate"):  # déchargement de la VRAM avant l'image (#245)
            return b'{"done": true}'
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
    return {
        "OLLAMA_URL": URL,
        "PIPER_BIN": str(binaire),
        "PIPER_MODEL": str(modele),
        "COMFY_URL": COMFY,
    }


# Scénario: étapes injectées


def test_cinq_etapes_presentes_et_classification_par_ollama(
    env: dict[str, str], tmp_path: Path
) -> None:
    faux = FauxOllama()
    etapes = etapes_reelles(env, out=tmp_path, transport=faux)
    assert isinstance(etapes, Etapes)
    assert all(callable(e) for e in (etapes.classifier, etapes.storyboard, etapes.voix))
    assert all(callable(e) for e in (etapes.image, etapes.clip, etapes.montage))
    assert etapes.classifier(DICTEE) == LIBRE
    assert len(faux.requetes) == 1
    assert faux.requetes[0]["messages"][1]["content"].startswith("<dictee>")


# Scénarios: configuration absente (échec fermé, aucun appel réseau)


@pytest.mark.parametrize("variable", ["OLLAMA_URL", "PIPER_BIN", "PIPER_MODEL", "COMFY_URL"])
def test_configuration_absente_leve_avant_tout_appel(
    env: dict[str, str], tmp_path: Path, variable: str
) -> None:
    del env[variable]
    faux = FauxOllama()
    with pytest.raises(ErreurConfiguration, match=variable):
        etapes_reelles(env, out=tmp_path, transport=faux)
    assert faux.requetes == []


# Scénario: rendu en échec ou trop long (#245), via `lancer` et un faux ComfyUI


def _comfy(faux: FauxComfy) -> ClientComfy:
    horloge = Horloge()
    return ClientComfy(COMFY, transport=faux.transport, horloge=horloge, dormir=horloge.dormir)


@pytest.mark.parametrize("mode", ["refus", "echec", "jamais"])
@pytest.mark.parametrize(("cible", "etape"), [("SaveImage", "image"), ("SaveVideo", "clip")])
def test_rendu_en_echec_run_en_erreur_sans_fuite(
    env: dict[str, str],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    cible: str,
    etape: str,
) -> None:
    faux = FauxComfy(mode=mode, cible=cible)
    out = tmp_path / "jobs"
    etapes = etapes_reelles(env, out=out, transport=FauxOllama(), comfy=_comfy(faux))
    with caplog.at_level(logging.DEBUG):
        run = lancer(DICTEE, etapes, run_id="r245", workdir=str(out))
    RUN.validate(run)
    assert run["etat"] == "erreur"
    assert (run["etapes"][-1]["nom"], run["etapes"][-1]["statut"]) == (etape, "erreur")
    assert "video_path" not in run
    assert not (out / "r245" / "final.mp4").exists()
    prompts = [p[c] for p in STORYBOARD["plans"] for c in ("image_prompt", "motion_prompt")]
    for texte in (json.dumps(run), caplog.text):
        assert not [p for p in prompts if p in texte]
        assert "hibou" not in texte


def test_comfy_url_hors_boucle_locale_refusee_avant_tout_appel(
    env: dict[str, str], tmp_path: Path
) -> None:
    env["COMFY_URL"] = "http://192.168.1.14:8188"
    faux = FauxOllama()
    with pytest.raises(ErreurConfiguration, match="COMFY_URL"):
        etapes_reelles(env, out=tmp_path, transport=faux)
    assert faux.requetes == []


def test_image_et_clip_branches_sur_comfyui(env: dict[str, str], tmp_path: Path) -> None:
    faux = FauxComfy()
    etapes = etapes_reelles(env, out=tmp_path, transport=FauxOllama(), comfy=_comfy(faux))
    assert etapes.image("r245-img-1", "an owl") == "r245-img-1.png"
    job = {
        "schema_version": "1.0",
        "job_id": "r245-clip-1",
        "type": "image_to_clip",
        "image_path": "r245-img-1.png",
        "motion_prompt": "the owl flies",
        "frames": 49,
    }
    assert Path(etapes.clip(job)).parent == tmp_path / "r245"
