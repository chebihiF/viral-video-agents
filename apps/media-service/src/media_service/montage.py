"""Montage FFmpeg minimal : concat des clips, voix off AAC, sous-titres ASS ou SRT incrustés.

Avec `subtitles_path` : ASS mot par mot (#52) ; sinon SRT.

Contrat d'entrée : contracts/montage-job.schema.json. Stdlib uniquement ; FFmpeg et
ffprobe système sont appelés avec une liste d'arguments (jamais de shell) et des chemins
absolus, donc jamais pris pour des options.
"""

import json
import math
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
WIDTH, HEIGHT, FPS = 1080, 1920, 24
SUBTITLE_STYLE = "Alignment=2,MarginV=25,FontSize=12,Outline=2"
FFMPEG_TIMEOUT_S = 900
PROBE_TIMEOUT_S = 30
STDERR_TAIL = 600


class MontageError(Exception):
    """Montage impossible ; le message nomme le plan ou l'étape en cause."""


@dataclass(frozen=True)
class Plan:
    clip_path: Path
    audio_path: Path
    narration_fr: str


@dataclass(frozen=True)
class MontageJob:
    plans: tuple[Plan, ...]
    output_path: Path
    subtitles_path: Path | None = None


@dataclass(frozen=True)
class MontageResult:
    video_path: Path
    srt_path: Path


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise MontageError(f"{where} : chaîne non vide sans caractère nul attendue")
    return value


def _exact_keys(data: Any, keys: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or set(data) != keys:
        raise MontageError(f"{where} : objet avec exactement les clés {sorted(keys)} attendu")
    return data


def parse_job(data: Any) -> MontageJob:
    """Valide un montage-job (contrat v1.0) ; refuse tout écart."""
    optional = {"subtitles_path"} & set(data) if isinstance(data, Mapping) else set()
    root = _exact_keys(data, {"schema_version", "plans", "output_path"} | optional, "montage-job")
    subtitles = Path(_text(root["subtitles_path"], "subtitles_path")) if optional else None
    if root["schema_version"] != SCHEMA_VERSION:
        raise MontageError(f"schema_version {root['schema_version']!r} non supportée")
    raw_plans = root["plans"]
    if not isinstance(raw_plans, list) or not raw_plans:
        raise MontageError("plans : liste non vide attendue")
    plans: list[Plan] = []
    for n, raw in enumerate(raw_plans, start=1):
        item = _exact_keys(raw, {"clip_path", "audio_path", "narration_fr"}, f"plan {n}")
        plans.append(
            Plan(
                Path(_text(item["clip_path"], f"plan {n} clip_path")),
                Path(_text(item["audio_path"], f"plan {n} audio_path")),
                _text(item["narration_fr"], f"plan {n} narration_fr"),
            )
        )
    output = _text(root["output_path"], "output_path")
    if not output.endswith(".mp4"):
        raise MontageError("output_path : extension .mp4 attendue")
    return MontageJob(plans=tuple(plans), output_path=Path(output), subtitles_path=subtitles)


def format_srt_time(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    h, rest = divmod(total_ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_MARKUP = str.maketrans("{}<>", "()\u2039\u203a")


def build_srt(narrations: list[str], durations: list[float]) -> str:
    """Un cue par plan ; sauts de ligne aplatis, balises ASS/HTML neutralisées."""
    cues: list[str] = []
    start = 0.0
    for n, (text, duration) in enumerate(zip(narrations, durations, strict=True), start=1):
        end = start + duration
        line = " ".join(text.split()).translate(_MARKUP)  # libass lit {\\an8} et <b>
        cues.append(f"{n}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{line}\n")
        start = end
    return "\n".join(cues)


def escape_filter_value(value: str) -> str:
    """Échappe une valeur d'option de filtre : niveau option, puis niveau graphe."""
    level1 = value.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    level2 = level1.replace("\\", "\\\\").replace("'", "\\'")
    for char in "[],;":
        level2 = level2.replace(char, "\\" + char)
    return level2


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))  # absolu : commence par « / », jamais une option


def _run(cmd: list[str], timeout: int, what: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - liste d'arguments, pas de shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise MontageError(f"{what} introuvable : installer FFmpeg sur le système") from exc
    except subprocess.TimeoutExpired as exc:
        raise MontageError(f"{what} : délai de {timeout} s dépassé") from exc


def probe_duration(audio: Path) -> float:
    """Durée en secondes d'un fichier audio, via ffprobe."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json"]
    done = _run([*cmd, "-i", str(audio)], PROBE_TIMEOUT_S, "ffprobe")
    if done.returncode != 0:
        raise MontageError(f"ffprobe a échoué pour {audio} : {done.stderr.strip()[-STDERR_TAIL:]}")
    try:
        duration = float(json.loads(done.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise MontageError(f"ffprobe : durée illisible pour {audio}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MontageError(f"ffprobe : durée invalide pour {audio}")
    return duration


def _check_inputs(job: MontageJob) -> MontageJob:
    out_dir = _absolute(job.output_path).parent
    if not out_dir.is_dir():
        raise MontageError(f"dossier de sortie introuvable : {out_dir}")
    for target in (_absolute(job.output_path), _absolute(job.output_path).with_suffix(".srt")):
        if target.is_dir():
            raise MontageError(f"sortie invalide : {target} est un dossier")
    subtitles = job.subtitles_path
    if subtitles is not None:
        if not subtitles.is_file():
            raise MontageError(f"sous-titres ASS introuvables : {subtitles}")
        subtitles = _absolute(subtitles)
    plans: list[Plan] = []
    for n, plan in enumerate(job.plans, start=1):
        for label, path in (("clip", plan.clip_path), ("audio", plan.audio_path)):
            if not path.is_file():
                raise MontageError(f"plan {n} : {label} introuvable : {path}")
        plans.append(Plan(_absolute(plan.clip_path), _absolute(plan.audio_path), plan.narration_fr))
    return MontageJob(tuple(plans), _absolute(job.output_path), subtitles)


def _filter_graph(durations: list[float], srt: Path, ass: Path | None = None) -> str:
    parts: list[str] = []
    for i, d in enumerate(durations):
        parts.append(
            f"[{2 * i}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS},trim=duration={d:.3f},"
            f"setpts=PTS-STARTPTS[v{i}]"
        )
        parts.append(
            f"[{2 * i + 1}:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"atrim=duration={d:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
    labels = "".join(f"[v{i}][a{i}]" for i in range(len(durations)))
    if ass is not None:  # le style vient du fichier ASS (#52), pas de force_style
        subs = f"ass=filename={escape_filter_value(str(ass))}"
    else:
        subs = (
            f"subtitles=filename={escape_filter_value(str(srt))}"
            f":force_style={escape_filter_value(SUBTITLE_STYLE)}"
        )
    parts.append(f"{labels}concat=n={len(durations)}:v=1:a=1[vc][ac]")
    parts.append(f"[vc]{subs}[v]")
    return ";".join(parts)


def _ffmpeg_command(job: MontageJob, durations: list[float], srt: Path, out: Path) -> list[str]:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    for plan, d in zip(job.plans, durations, strict=True):
        cmd += ["-stream_loop", "-1", "-t", f"{d:.3f}", "-i", str(plan.clip_path)]
        cmd += ["-i", str(plan.audio_path)]
    graph = _filter_graph(durations, srt, job.subtitles_path)
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[ac]"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20"]
    cmd += ["-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    return [*cmd, "-f", "mp4", str(out)]


def _install(video: Path, srt: Path, final_video: Path, final_srt: Path, work: Path) -> None:
    """MP4 puis SRT ; si le SRT échoue, l'ancienne paire est restaurée (jamais un mélange)."""
    backup = work / "previous.mp4"
    had_previous = final_video.exists()
    try:
        if had_previous:
            os.replace(final_video, backup)
        os.replace(video, final_video)
        os.replace(srt, final_srt)
    except OSError as exc:
        final_video.unlink(missing_ok=True)
        if had_previous and backup.exists():
            os.replace(backup, final_video)
        raise MontageError(f"installation de la sortie impossible : {exc}") from exc


def run_montage(job: MontageJob) -> MontageResult:
    """Assemble le MP4 et son SRT ; écriture atomique, aucun fichier partiel en cas d'échec."""
    job = _check_inputs(job)
    durations: list[float] = []
    for n, plan in enumerate(job.plans, start=1):
        try:
            durations.append(probe_duration(plan.audio_path))
        except MontageError as exc:
            raise MontageError(f"plan {n} : audio illisible : {exc}") from exc

    final_video = job.output_path
    final_srt = final_video.with_suffix(".srt")
    work = Path(tempfile.mkdtemp(prefix=".montage-", dir=final_video.parent))
    try:
        srt = work / "subtitles.srt"
        srt.write_text(build_srt([p.narration_fr for p in job.plans], durations), encoding="utf-8")
        video = work / "montage.mp4"
        done = _run(_ffmpeg_command(job, durations, srt, video), FFMPEG_TIMEOUT_S, "ffmpeg")
        if done.returncode != 0:
            raise MontageError(f"ffmpeg a échoué : {done.stderr.strip()[-STDERR_TAIL:]}")
        _install(video, srt, final_video, final_srt, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return MontageResult(video_path=final_video, srt_path=final_srt)
