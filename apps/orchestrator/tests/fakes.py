"""Doublures pour le parcours de bout en bout (#139, #249) : faux SILMA, clips lavfi, GPU tracé.

Rien ici n'appelle un réseau ni un GPU ; seul ffmpeg local est utilisé (clips `lavfi`).
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.mocks import etapes_factices
from orchestrator.pipeline import Etapes
from orchestrator.steps_media import montage_reel, voix_silma

SECONDES_PAR_MOT = 0.25  # durée d'un WAV du faux SILMA : un quart de seconde par mot
VOIX_OFF = "narratrice"

# Faux moteur SILMA (même interface que scripts/silma_cli.py) : entrée JSON sur stdin, argv
# consignés dans <binaire>.log (jamais le texte), WAV silencieux de 0,25 s par mot.
FAUX_SILMA = f"""#!{sys.executable}
import json, sys, wave
from pathlib import Path
args = sys.argv[1:]
entree = json.loads(sys.stdin.read())
with open(Path(sys.argv[0]).with_suffix(".log"), "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\\n")
out = args[args.index("--sortie") + 1]
rate = 24000
with wave.open(out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(b"\\0\\0" * (rate // 4) * len(entree["texte"].split()))
"""


def env_faux_silma(dossier: Path, moteur: str = FAUX_SILMA) -> dict[str, str]:
    """Faux moteur, dossier de poids vide, une voix de référence synthétique (`VOIX_OFF`).

    Rend `SILMA_BIN`, `SILMA_MODEL_DIR`, `SILMA_VOIX` et `VOIX_OFF_ID`. Aucun fichier audio du
    dépôt : la référence est un WAV silencieux écrit ici.
    """
    dossier.mkdir(parents=True, exist_ok=True)
    binaire = dossier / "silma"
    binaire.write_text(moteur, encoding="utf-8")
    binaire.chmod(binaire.stat().st_mode | stat.S_IXUSR)
    (dossier / "poids").mkdir(exist_ok=True)
    reference = dossier / f"{VOIX_OFF}.wav"
    with wave.open(str(reference), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000)
    (dossier / f"{VOIX_OFF}.txt").write_text("transcription de la référence", encoding="utf-8")
    voix = {VOIX_OFF: {"audio": reference.name, "transcription": f"{VOIX_OFF}.txt"}}
    (dossier / "voix.json").write_text(json.dumps(voix), encoding="utf-8")
    return {
        "SILMA_BIN": str(binaire),
        "SILMA_MODEL_DIR": str(dossier / "poids"),
        "SILMA_VOIX": str(dossier / "voix.json"),
        "VOIX_OFF_ID": VOIX_OFF,
    }


def appels_faux_silma(env: Mapping[str, str]) -> list[list[str]]:
    """argv de chaque lancement du faux moteur (vide s'il n'a jamais été lancé)."""
    log = Path(env["SILMA_BIN"]).with_suffix(".log")
    lignes = log.read_text("utf-8").splitlines() if log.exists() else []
    return [json.loads(ligne) for ligne in lignes]


@dataclass
class TraceGpu:
    """Intervalles (début, fin) des étapes GPU factices, tous runs confondus."""

    intervalles: list[tuple[str, float, float]] = field(default_factory=list)
    _verrou: threading.Lock = field(default_factory=threading.Lock)

    def mesurer(self, nom: str, pause_s: float = 0.02) -> None:
        debut = time.monotonic()
        time.sleep(pause_s)  # laisse un éventuel second run empiéter si le verrou manquait
        self.ajouter(nom, debut, time.monotonic())

    def ajouter(self, nom: str, debut: float, fin: float) -> None:
        with self._verrou:
            self.intervalles.append((nom, debut, fin))

    def chevauchements(self) -> list[tuple[str, str]]:
        """Paires d'étapes dont les intervalles se recouvrent (liste vide = série)."""
        triees = sorted(self.intervalles, key=lambda i: i[1])
        return [(a[0], b[0]) for a, b in zip(triees, triees[1:], strict=False) if b[1] < a[2]]


def etapes_e2e(
    dossier: Path,
    *,
    env_silma: Mapping[str, str],
    trace: TraceGpu | None = None,
    montage: Callable[[Mapping[str, Any]], str] | None = None,
) -> Etapes:
    """Étapes texte et GPU factices (clips lavfi), voix (faux SILMA) et montage FFmpeg réels."""
    base = etapes_factices()
    trace = trace or TraceGpu()
    dossier.mkdir(parents=True, exist_ok=True)

    def image(job_id: str, image_prompt: str) -> str:
        trace.mesurer(f"image:{job_id}")
        return f"{job_id}.png"  # chemin relatif conforme au gpu-job ; aucun fichier nécessaire

    def clip(job: Mapping[str, Any]) -> str:
        cible = dossier / f"{job['job_id']}.mp4"
        _clip_lavfi(cible)
        trace.mesurer(f"clip:{job['job_id']}")
        return str(cible)

    voix = voix_silma(env_silma)

    def voix_tracee(job: Mapping[str, Any]) -> Mapping[str, Any]:
        debut = time.monotonic()
        resultat = voix(job)
        trace.ajouter("voix", debut, time.monotonic())
        return resultat

    return Etapes(
        base.classifier,
        base.storyboard,
        voix_tracee,
        image,
        clip,
        montage or montage_reel(),
        voix_off_id=env_silma["VOIX_OFF_ID"],
    )


def _clip_lavfi(cible: Path) -> None:
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=24:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(cible),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
