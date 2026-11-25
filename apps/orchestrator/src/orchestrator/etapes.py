"""Assemblage des étapes réelles (#150, #245) : Ollama, Piper, ComfyUI (image, clip), montage.

Échec fermé : `ErreurConfiguration` (OLLAMA_URL, PIPER_BIN, PIPER_MODEL, COMFY_URL hors boucle
locale, prompts, schémas, workflows) est levée à l'assemblage, avant tout appel réseau. Les clips
sont téléchargés sous `<out>/<run_id>/`. Requiert Python 3.12 et l'extra `media` (media-service).
Aucun texte de dictée ni prompt dans les messages.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from orchestrator.pipeline import Etapes
from orchestrator.steps_gpu import ClientComfy, EtapesGpu, comfy_depuis_env, decharger_ollama
from orchestrator.steps_llm import (
    Transport,
    classifier_ollama,
    client_depuis_env,
    storyboard_ollama,
)
from orchestrator.steps_llm import _post_urllib as _transport_http
from orchestrator.steps_media import _verifier_configuration, montage_reel, voix_piper

__all__ = ["etapes_reelles"]


def etapes_reelles(
    env: Mapping[str, str] | None = None,
    *,
    out: Path,
    transport: Transport = _transport_http,
    comfy: ClientComfy | None = None,
) -> Etapes:
    """Étapes réelles ; lève `ErreurConfiguration` si Ollama, Piper ou ComfyUI est mal configuré.

    `transport` (Ollama) et `comfy` sont injectables : faux serveurs en test.
    """
    environnement = os.environ if env is None else env
    client = client_depuis_env(environnement, transport)
    classifier = classifier_ollama(client)
    storyboard = storyboard_ollama(client)
    _verifier_configuration(environnement)
    comfy = comfy_depuis_env(environnement) if comfy is None else comfy
    gpu = EtapesGpu(comfy, decharger_ollama(client), out)
    voix = voix_piper(environnement)
    return Etapes(classifier, storyboard, voix, gpu.image, gpu.clip, montage_reel())
