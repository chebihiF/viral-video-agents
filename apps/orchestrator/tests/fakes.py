"""Doublures pour le parcours de bout en bout (#139) : faux Piper, clips lavfi, GPU tracé.

Rien ici n'appelle un réseau ni un GPU ; seul ffmpeg local est utilisé (clips `lavfi`).
"""

from __future__ import annotations

import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.mocks import etapes_factices
from orchestrator.pipeline import Etapes
from orchestrator.steps_media import montage_reel, voix_piper

SECONDES_PAR_MOT = 0.25  # durée d'un WAV du faux Piper : un quart de seconde par mot

FAUX_PIPER = f"""#!{sys.executable}
import sys, wave
out = sys.argv[sys.argv.index("--output_file") + 1]
text = sys.stdin.read()
rate = 24000
with wave.open(out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(b"\\0\\0" * (rate // 4) * len(text.split()))
"""


def env_faux_piper(dossier: Path) -> dict[str, str]:
    """Écrit un faux binaire Piper et un faux modèle ; rend `PIPER_BIN` / `PIPER_MODEL`."""
    binaire = dossier / "piper"
    binaire.write_text(FAUX_PIPER, encoding="utf-8")
    binaire.chmod(binaire.stat().st_mode | stat.S_IXUSR)
    modele = dossier / "voix.onnx"
    modele.write_bytes(b"model")
    return {"PIPER_BIN": str(binaire), "PIPER_MODEL": str(modele)}


@dataclass
class TraceGpu:
    """Intervalles (début, fin) des étapes GPU factices, tous runs confondus."""

    intervalles: list[tuple[str, float, float]] = field(default_factory=list)
    _verrou: threading.Lock = field(default_factory=threading.Lock)

    def mesurer(self, nom: str, pause_s: float = 0.02) -> None:
        debut = time.monotonic()
        time.sleep(pause_s)  # laisse un éventuel second run empiéter si le verrou manquait
        fin = time.monotonic()
        with self._verrou:
            self.intervalles.append((nom, debut, fin))

    def chevauchements(self) -> list[tuple[str, str]]:
        """Paires d'étapes dont les intervalles se recouvrent (liste vide = série)."""
        triees = sorted(self.intervalles, key=lambda i: i[1])
        return [(a[0], b[0]) for a, b in zip(triees, triees[1:], strict=False) if b[1] < a[2]]


def etapes_e2e(
    dossier: Path,
    *,
    env_piper: Mapping[str, str],
    trace: TraceGpu | None = None,
    montage: Callable[[Mapping[str, Any]], str] | None = None,
) -> Etapes:
    """Étapes texte et GPU factices (clips lavfi), voix Piper et montage FFmpeg réels."""
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

    return Etapes(
        base.classifier,
        base.storyboard,
        voix_piper(env_piper),
        image,
        clip,
        montage or montage_reel(),
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
