"""Assemblage des étapes réelles (#150, #245, #249) : Ollama, SILMA TTS, ComfyUI, montage.

Échec fermé : `ErreurConfiguration` (OLLAMA_URL, SILMA_BIN, SILMA_MODEL_DIR, SILMA_VOIX,
VOIX_OFF_ID, COMFY_URL hors boucle locale, prompts, schémas, workflows) est levée à l'assemblage,
avant tout appel réseau ni lancement du moteur. La voix (SILMA, sur le GPU) suit `/free` de
ComfyUI et le déchargement d'Ollama ; le pipeline la lance sous le verrou GPU. Les clips sont
téléchargés sous `<out>/<run_id>/`. Requiert Python 3.12 et l'extra `media` (media-service).
Aucun texte de dictée, prompt ni chemin dans les messages.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from orchestrator.pipeline import Etapes, Job
from orchestrator.steps_gpu import ClientComfy, EtapesGpu, comfy_depuis_env, decharger_ollama
from orchestrator.steps_llm import (
    ErreurConfiguration,
    Transport,
    classifier_ollama,
    client_depuis_env,
    storyboard_ollama,
)
from orchestrator.steps_llm import _post_urllib as _transport_http
from orchestrator.steps_media import _verifier_configuration, montage_reel, voix_silma

__all__ = ["etapes_reelles"]


def etapes_reelles(
    env: Mapping[str, str] | None = None,
    *,
    out: Path,
    transport: Transport = _transport_http,
    comfy: ClientComfy | None = None,
    decharge: Callable[[], None] | None = None,
) -> Etapes:
    """Étapes réelles ; lève `ErreurConfiguration` si Ollama, SILMA ou ComfyUI est mal configuré.

    `transport` (Ollama), `comfy` et `decharge` (VRAM d'Ollama) sont injectables : faux en test.
    """
    environnement = os.environ if env is None else env
    client = client_depuis_env(environnement, transport)
    classifier = classifier_ollama(client)
    storyboard = storyboard_ollama(client)
    voix_off = _voix_off(environnement)
    comfy = comfy_depuis_env(environnement) if comfy is None else comfy
    decharge = decharger_ollama(client.url) if decharge is None else decharge
    gpu = EtapesGpu(comfy, decharge, out)
    voix = _apres(gpu.avant_voix, voix_silma(environnement))
    montage = montage_reel()
    return Etapes(classifier, storyboard, voix, gpu.image, gpu.clip, montage, voix_off)


def _voix_off(env: Mapping[str, str]) -> str:
    """Voix provisoire de chaque réplique (#250) : VOIX_OFF_ID, configurée dans SILMA_VOIX."""
    voix = _verifier_configuration(env).voix
    voix_off = env.get("VOIX_OFF_ID", "").strip()
    if not voix_off:
        raise ErreurConfiguration("VOIX_OFF_ID non configuré : voix_id de la voix off")
    if voix_off not in voix:
        raise ErreurConfiguration("VOIX_OFF_ID absent de SILMA_VOIX : aucune voix de repli")
    return voix_off


def _apres(preparer: Callable[[], None], etape: Callable[[Job], Job]) -> Callable[[Job], Job]:
    def voix(job: Job) -> Job:
        preparer()
        return etape(job)

    return voix
