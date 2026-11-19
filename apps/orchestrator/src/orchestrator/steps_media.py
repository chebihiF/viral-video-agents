"""Étapes média réelles : voix (Piper) et montage (ASS + FFmpeg), sans GPU (#136).

`voix_piper` : tts-job -> TtsResult (`media_service.tts`). `montage_reel` : montage-job -> chemin du
MP4 ; écrit l'ASS mot par mot à côté du MP4 puis l'incruste (`subtitles_path`, #135).
Requiert Python 3.12 et l'extra `media` (media-service). Piper : PIPER_BIN / PIPER_MODEL.
Aucune narration dans les logs ni les exceptions : celles du montage sont réécrites sans le
message de FFmpeg (qui peut citer le texte) et levées hors de tout `except` (ni cause ni contexte).
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from media_service.montage import MontageError, probe_duration, run_montage
from media_service.montage import parse_job as parse_montage_job
from media_service.subtitles import ShotText, SubtitleError, write_ass
from media_service.tts import SCHEMA_VERSION, TtsError, _config, synthetiser
from media_service.tts import parse_job as parse_tts_job
from orchestrator.pipeline import Job
from orchestrator.steps_llm import ErreurConfiguration

log = logging.getLogger(__name__)

__all__ = ["ErreurMontage", "ErreurVoix", "montage_reel", "voix_piper"]


class ErreurVoix(Exception):
    """Synthèse vocale impossible (message du media-service : plan nommé, jamais son texte)."""


class ErreurMontage(Exception):
    """Montage impossible (message générique : jamais de texte de narration)."""


def voix_piper(env: Mapping[str, str] | None = None) -> Callable[[Job], Job]:
    """Étape `voix` : un WAV par plan avec Piper, rend un TtsResult (tts-job.schema.json)."""

    def voix(tts_job: Job) -> Job:
        environnement = os.environ if env is None else env
        _verifier_configuration(environnement)
        job = _sans_contexte(lambda: parse_tts_job(tts_job), TtsError, ErreurVoix, "tts-job")
        Path(job.output_dir).mkdir(parents=True, exist_ok=True)
        voix = _sans_contexte(
            lambda: synthetiser(job.plans, job.output_dir, env=environnement),
            TtsError,
            ErreurVoix,
            "synthèse",
        )
        log.info("voix plans=%d", len(voix))
        return {
            "schema_version": SCHEMA_VERSION,
            "plans": [
                {"plan_id": v.plan_id, "wav_path": str(v.wav_path), "duree_s": v.duree_s}
                for v in voix
            ],
        }

    return voix


def montage_reel() -> Callable[[Job], str]:
    """Étape `montage` : ASS mot par mot écrit à côté du MP4, puis MP4 1080x1920 sous-titré."""

    def montage(montage_job: Job) -> str:
        job = _sans_contexte(
            lambda: parse_montage_job(montage_job), MontageError, ErreurMontage, "montage-job"
        )
        ass = job.output_path.with_suffix(".ass")
        try:
            job.output_path.parent.mkdir(parents=True, exist_ok=True)
            durees = [probe_duration(p.audio_path) for p in job.plans]
            formes = [ShotText(p.narration_fr, d) for p, d in zip(job.plans, durees, strict=True)]
            write_ass(formes, ass)
            resultat = run_montage(dataclasses.replace(job, subtitles_path=ass))
        except (MontageError, SubtitleError, OSError) as exc:
            ass.unlink(missing_ok=True)
            echec = type(exc).__name__
        except BaseException:
            ass.unlink(missing_ok=True)
            raise
        else:
            log.info("montage plans=%d", len(job.plans))
            return str(resultat.video_path)
        raise ErreurMontage(f"montage : échec ({echec})")

    return montage


def _verifier_configuration(env: Mapping[str, str]) -> None:
    try:
        _config(env)
    except TtsError as exc:
        message = str(exc)  # « PIPER_BIN … » / « PIPER_MODEL … » : jamais de texte de dictée
    else:
        return
    raise ErreurConfiguration(message)


def _sans_contexte(
    appel: Callable[[], Any], source: type[Exception], cible: type[Exception], quoi: str
) -> Any:
    """Appelle `appel` ; toute `source` devient `cible` sans cause ni contexte, message neutre."""
    try:
        return appel()
    except source as exc:
        message = f"{quoi} : {_message_sur(exc, source)}"
    raise cible(message)


def _message_sur(exc: Exception, source: type[Exception]) -> str:
    """Les messages TtsError nomment le plan, jamais son texte ; ceux du montage sont masqués."""
    return str(exc) if source is TtsError else type(exc).__name__
