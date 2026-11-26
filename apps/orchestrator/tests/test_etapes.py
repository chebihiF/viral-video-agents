"""Étapes réelles assemblées (#150, #245, #249) : faux Ollama, SILMA, ComfyUI ; ni réseau ni GPU."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fakes import VOIX_OFF, appels_faux_silma, env_faux_silma
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
    return {"OLLAMA_URL": URL, "COMFY_URL": COMFY, **env_faux_silma(tmp_path / "silma")}


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


VARIABLES = ["OLLAMA_URL", "SILMA_BIN", "SILMA_MODEL_DIR", "SILMA_VOIX", "VOIX_OFF_ID", "COMFY_URL"]


@pytest.mark.parametrize("variable", VARIABLES)
def test_configuration_absente_leve_avant_tout_appel(
    env: dict[str, str], tmp_path: Path, variable: str
) -> None:
    del env[variable]
    faux = FauxOllama()
    with pytest.raises(ErreurConfiguration, match=variable) as err:
        etapes_reelles(env, out=tmp_path, transport=faux)
    assert str(tmp_path) not in str(err.value)
    assert faux.requetes == []
    assert appels_faux_silma({**env, "SILMA_BIN": str(tmp_path / "silma" / "silma")}) == []


@pytest.mark.parametrize("variable", ["SILMA_BIN", "SILMA_MODEL_DIR", "SILMA_VOIX"])
def test_fichier_silma_introuvable_nomme_la_variable_sans_chemin(
    env: dict[str, str], tmp_path: Path, variable: str
) -> None:
    env[variable] = str(tmp_path / "introuvable")
    faux = FauxOllama()
    with pytest.raises(ErreurConfiguration, match=variable) as err:
        etapes_reelles(env, out=tmp_path, transport=faux)
    assert str(tmp_path) not in str(err.value) and "/" not in str(err.value)
    assert faux.requetes == []


@pytest.mark.parametrize("valeur", ["renard", "../voix", " "])
def test_voix_off_non_configuree_dans_silma_voix(
    env: dict[str, str], tmp_path: Path, valeur: str
) -> None:
    env["VOIX_OFF_ID"] = valeur
    with pytest.raises(ErreurConfiguration, match="VOIX_OFF_ID"):
        etapes_reelles(env, out=tmp_path, transport=FauxOllama())


# Scénario (#249): un seul modèle lourd à la fois


def test_voix_apres_free_et_dechargement_d_ollama(env: dict[str, str], tmp_path: Path) -> None:
    journal: list[str] = []
    faux = FauxComfy(journal)

    def decharge() -> None:
        journal.append(f"ollama decharge (moteur lancé {len(appels_faux_silma(env))} fois)")

    etapes = etapes_reelles(
        env, out=tmp_path, transport=FauxOllama(), comfy=_comfy(faux), decharge=decharge
    )
    assert etapes.voix_off_id == VOIX_OFF
    job = {
        "schema_version": "2.0",
        "output_dir": str(tmp_path / "voix"),
        "repliques": [
            {"replique_id": "r1", "voix_id": VOIX_OFF, "texte": "un deux", "langue": "ar"}
        ],
    }
    resultat = etapes.voix(job)
    assert journal == ["comfy POST /free", "ollama decharge (moteur lancé 0 fois)"]
    assert len(appels_faux_silma(env)) == 1
    assert [r["replique_id"] for r in resultat["repliques"]] == ["r1"]


# Scénario: rendu en échec ou trop long (#245), via `lancer` et un faux ComfyUI


def _sans_ollama() -> None:
    """Déchargement de la VRAM d'Ollama : testé dans test_steps_gpu et test_cli."""


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
    etapes = etapes_reelles(
        env, out=out, transport=FauxOllama(), comfy=_comfy(faux), decharge=_sans_ollama
    )
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
    etapes = etapes_reelles(
        env, out=tmp_path, transport=FauxOllama(), comfy=_comfy(faux), decharge=_sans_ollama
    )
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
