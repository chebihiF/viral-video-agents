"""Étapes factices valides (#138) : aucune génération réelle, aucun fichier écrit, pas de GPU.

Sorties conformes aux contrats ; aucune ne reprend le texte de la dictée.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestrator.pipeline import Etapes

MOT_BLOQUE = "MOCK-BLOQUE"  # mot-clé de test : le faux classifieur classe la dictée « bloque »

_PLANS = [
    {
        "image_prompt": "A cute orange cat on a flying carpet, 3D animated film style",
        "motion_prompt": "The carpet glides forward gently, slow camera dolly in",
        "narration_fr": "Le petit chat s'envole sur son tapis magique !",
    },
    {
        "image_prompt": "The orange cat waving at colorful birds in a blue sky",
        "motion_prompt": "The cat waves, the birds flap their wings",
        "narration_fr": "Bonjour les oiseaux, crie-t-il en riant.",
    },
]


def _classifier(dictee: str) -> Mapping[str, Any]:
    if MOT_BLOQUE in dictee:
        return {
            "schema_version": "1",
            "classe": "bloque",
            "signaler_parent": True,
            "motif": "Mot-clé de test : rien n'est généré.",
            "mot_pour_enfant": "Cette idée-là, on ne peut pas la dessiner.",
        }
    return {"schema_version": "1", "classe": "libre", "signaler_parent": False}


def _storyboard(texte: str) -> Mapping[str, Any]:
    return {
        "schema_version": "1.0",
        "compris_fr": "Un petit chat orange vole sur un tapis magique.",
        "recap_darija": "Mech sghir kaytir f sma 3la zerbiya sehriya.",
        "classification": "libre",
        "plans": [dict(p) for p in _PLANS],
    }


def _voix(job: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "plans": [
            {"plan_id": p["plan_id"], "wav_path": f"{job['output_dir']}/{p['plan_id']}.wav"}
            for p in job["plans"]
        ]
    }


def _image(job_id: str, image_prompt: str) -> str:
    return f"{job_id}.png"


def _clip(job: Mapping[str, Any]) -> str:
    return f"{job['job_id']}.mp4"


def _montage(job: Mapping[str, Any]) -> str:
    return str(job["output_path"])


def etapes_factices() -> Etapes:
    """Jeu d'étapes factices pour `--mocks`."""
    return Etapes(_classifier, _storyboard, _voix, _image, _clip, _montage)
