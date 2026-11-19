"""Assemblage des étapes réelles (#150) : Ollama, Piper, montage ; image et clip injectables.

Échec fermé : `ErreurConfiguration` (OLLAMA_URL, PIPER_BIN, PIPER_MODEL, prompts, schémas) est levée
à l'assemblage, avant tout appel réseau. Image et clip dépendent du gpu-worker (#33), pas encore
branché : par défaut ils lèvent `ErreurConfiguration`, le run finit « erreur » sans MP4 partiel.
Requiert Python 3.12 et l'extra `media` (media-service). Aucun texte de dictée dans les messages.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from orchestrator.pipeline import Etapes, Job
from orchestrator.steps_llm import (
    ErreurConfiguration,
    Transport,
    classifier_ollama,
    client_depuis_env,
    storyboard_ollama,
)
from orchestrator.steps_llm import _post_urllib as _transport_http
from orchestrator.steps_media import _verifier_configuration, montage_reel, voix_piper

__all__ = ["etapes_reelles"]

Image = Callable[[str, str], str]
Clip = Callable[[Job], str]

_GPU_ABSENT = "gpu-worker non configuré : étape %s non branchée (#33)"


def _image_non_branchee(job_id: str, image_prompt: str) -> str:
    raise ErreurConfiguration(_GPU_ABSENT % "image")


def _clip_non_branche(job: Job) -> str:
    raise ErreurConfiguration(_GPU_ABSENT % "clip")


def etapes_reelles(
    env: Mapping[str, str] | None = None,
    *,
    transport: Transport = _transport_http,
    image: Image = _image_non_branchee,
    clip: Clip = _clip_non_branche,
) -> Etapes:
    """Étapes réelles ; lève `ErreurConfiguration` si Ollama ou Piper n'est pas configuré."""
    environnement = os.environ if env is None else env
    client = client_depuis_env(environnement, transport)
    classifier = classifier_ollama(client)
    storyboard = storyboard_ollama(client)
    _verifier_configuration(environnement)
    return Etapes(classifier, storyboard, voix_piper(environnement), image, clip, montage_reel())
