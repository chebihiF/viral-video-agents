"""Pipeline d'une dictée : classification, storyboard, voix, image, clip, montage (#53).

Chaque étape est une fonction injectée, conforme aux contrats de `contracts/`. La sortie suit
`contracts/pipeline-run.schema.json`. Étapes GPU (image, clip) sérialisées par un verrou commun à
tous les runs. Aucun texte (dictée, texte adouci, message d'exception) dans l'état ni les logs.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

log = logging.getLogger(__name__)

ETAPES = ("classification", "storyboard", "voix", "image", "clip", "montage")
SCHEMA_VERSION = "1.0"
GPU_LOCK = threading.Lock()  # une seule carte : un job ComfyUI à la fois, tous runs confondus
FRAMES = 49  # 2 s à 24 i/s ; le montage boucle le clip sur la durée de la voix
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,48}")
_IMAGE_PATH = re.compile(r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}")  # gpu-job image_path

Job = Mapping[str, Any]
Run = dict[str, Any]
T = TypeVar("T")


@dataclass(frozen=True)
class Etapes:
    """Étapes injectées, dans l'ordre d'appel."""

    classifier: Callable[[str], Job]  # dictée -> classification.schema.json
    storyboard: Callable[[str], Job]  # texte autorisé -> storyboard.schema.json
    voix: Callable[[Job], Job]  # tts-job.schema.json -> $defs/TtsResult
    image: Callable[[str, str], str]  # (job_id, image_prompt) -> image_path du gpu-job
    clip: Callable[[Job], str]  # gpu-job.schema.json -> chemin du clip
    montage: Callable[[Job], str]  # montage-job.schema.json -> MP4 final


class SortieNonConforme(Exception):
    """Sortie d'étape hors contrat (message sans contenu de l'étape)."""


class _EtapeEchouee(Exception):
    pass


def lancer(
    dictee: str,
    etapes: Etapes,
    *,
    run_id: str | None = None,
    workdir: str = "/data/jobs",
    gpu_lock: AbstractContextManager[Any] = GPU_LOCK,
    mode: str | None = None,
) -> Run:
    """Exécute le pipeline et rend l'état du run (pipeline-run v1.0).

    Ne lève pas si une étape échoue : le run est « erreur » et nomme l'étape.
    `mode` (reel|mocks) est recopié tel quel ; `termine_le` horodate la fin en UTC.
    """
    rid = run_id or uuid.uuid4().hex[:16]
    if not _RUN_ID.fullmatch(rid):
        raise ValueError("run_id invalide")
    run: Run = {"schema_version": SCHEMA_VERSION, "run_id": rid, "etat": "erreur", "etapes": []}
    if mode is not None:
        run["mode"] = mode
    try:
        texte = _etape(
            run, "classification", lambda: _texte_autorise(etapes.classifier(dictee), dictee)
        )
        if texte is None:
            run["etat"] = "bloque"
        else:
            run["video_path"] = _generer(run, texte, etapes, f"{workdir}/{rid}", gpu_lock)
            run["etat"] = "ok"
    except _EtapeEchouee:
        pass
    # timezone.utc plutôt que datetime.UTC : le module reste importable en Python 3.9.
    run["termine_le"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017
    log.info("run=%s etat=%s", rid, run["etat"])
    return run


def ligne_durees(run: Run) -> Run:
    """Ligne de `durees.jsonl` (pipeline-run $defs/LigneDurees) : durées par étape, sans texte."""
    etapes = [{k: e[k] for k in ("nom", "statut", "duree_s")} for e in run["etapes"]]
    cles = ("run_id", "mode", "etat", "termine_le")
    return {**{k: run[k] for k in cles}, "etapes": etapes}


# TypeVar plutôt que PEP 695 : le module reste importable en Python 3.9.
def _etape(run: Run, nom: str, fn: Callable[[], T]) -> T:  # noqa: UP047
    debut = time.monotonic()
    try:
        resultat = fn()
    except Exception as exc:
        _noter(run, nom, "erreur", debut)
        log.warning("run=%s etape=%s statut=erreur type=%s", run["run_id"], nom, type(exc).__name__)
        raise _EtapeEchouee(nom) from None
    _noter(run, nom, "ok", debut)
    return resultat


def _noter(run: Run, nom: str, statut: str, debut: float) -> None:
    duree = round(time.monotonic() - debut, 3)
    run["etapes"].append({"nom": nom, "statut": statut, "duree_s": duree})
    log.info("run=%s etape=%s statut=%s duree_s=%.3f", run["run_id"], nom, statut, duree)


def _texte_autorise(classification: Job, dictee: str) -> str | None:
    """Texte à mettre en scène : la dictée (libre), le texte adouci (adouci), rien (bloque)."""
    classe = classification.get("classe")
    if classe == "libre":
        return dictee
    if classe == "bloque":
        return None
    texte = classification.get("texte_adouci")
    if classe == "adouci" and isinstance(texte, str) and texte.strip():
        return texte
    raise SortieNonConforme("classification hors contrat")


def _generer(
    run: Run, texte: str, etapes: Etapes, dossier: str, gpu_lock: AbstractContextManager[Any]
) -> str:
    rid = run["run_id"]
    plans = _etape(run, "storyboard", lambda: _plans(etapes.storyboard(texte)))
    wavs = _etape(run, "voix", lambda: _wavs(etapes.voix(_tts_job(plans, dossier)), len(plans)))
    with gpu_lock:  # une prise par phase : les plans d'un run ne sont pas entrelacés
        images = _etape(run, "image", lambda: _images(etapes, rid, plans))
    with gpu_lock:
        clips = _etape(run, "clip", lambda: _clips(etapes, rid, plans, images))
    job = _montage_job(plans, wavs, clips, f"{dossier}/final.mp4")
    return _etape(run, "montage", lambda: _video(etapes.montage(job)))


def _plans(storyboard: Job) -> list[Job]:
    plans = storyboard.get("plans")
    cles = ("image_prompt", "motion_prompt", "narration_fr")
    if not isinstance(plans, list) or not plans:
        raise SortieNonConforme("storyboard sans plans")
    if not all(
        isinstance(p, Mapping) and all(isinstance(p.get(c), str) for c in cles) for p in plans
    ):
        raise SortieNonConforme("plan de storyboard hors contrat")
    return plans


def _tts_job(plans: list[Job], dossier: str) -> Job:
    return {
        "schema_version": "1.0",
        "output_dir": f"{dossier}/voix",
        "plans": [
            {"plan_id": _plan_id(n), "texte": p["narration_fr"], "langue": "fr"}
            for n, p in enumerate(plans, 1)
        ],
    }


def _plan_id(n: int) -> str:
    return f"plan-{n}"


def _wavs(resultat: Job, attendus: int) -> list[str]:
    """WAV associés aux plans par plan_id, dans l'ordre du storyboard."""
    plans = resultat.get("plans")
    if not isinstance(plans, list) or not all(isinstance(p, Mapping) for p in plans):
        raise SortieNonConforme("voix : plans hors contrat")
    wavs = {p.get("plan_id"): p.get("wav_path") for p in plans}
    ids = [_plan_id(n) for n in range(1, attendus + 1)]
    if len(plans) != attendus or sorted(wavs, key=str) != sorted(ids):
        raise SortieNonConforme("voix : plan_id sans correspondance")
    return [_chemin(wavs[i], "voix") for i in ids]


def _images(etapes: Etapes, rid: str, plans: list[Job]) -> list[str]:
    images = []
    for n, plan in enumerate(plans, 1):
        image = etapes.image(f"{rid}-img-{n}", plan["image_prompt"])
        if not isinstance(image, str) or not _IMAGE_PATH.fullmatch(image):
            raise SortieNonConforme("image : image_path hors contrat gpu-job")
        images.append(image)
    return images


def _clips(etapes: Etapes, rid: str, plans: list[Job], images: list[str]) -> list[str]:
    clips = []
    for n, plan in enumerate(plans, 1):
        job = {
            "schema_version": "1.0",
            "job_id": f"{rid}-clip-{n}",
            "type": "image_to_clip",
            "image_path": images[n - 1],
            "motion_prompt": plan["motion_prompt"],
            "frames": FRAMES,
        }
        clips.append(_chemin(etapes.clip(job), "clip"))
    return clips


def _chemin(chemin: object, etape: str) -> str:
    """Chemin de fichier non vide, sans NUL ni segment '..' (montage-job)."""
    if not isinstance(chemin, str) or not chemin.strip() or "\x00" in chemin:
        raise SortieNonConforme(f"{etape} : chemin hors contrat")
    if ".." in chemin.split("/"):
        raise SortieNonConforme(f"{etape} : chemin hors contrat")
    return chemin


def _montage_job(plans: list[Job], wavs: list[str], clips: list[str], sortie: str) -> Job:
    return {
        "schema_version": "1.0",
        "plans": [
            {"clip_path": clips[i], "audio_path": wavs[i], "narration_fr": p["narration_fr"]}
            for i, p in enumerate(plans)
        ],
        "output_path": sortie,
    }


def _video(chemin: str) -> str:
    if not isinstance(chemin, str) or not chemin.endswith(".mp4"):
        raise SortieNonConforme("montage : chemin MP4 attendu")
    return chemin
