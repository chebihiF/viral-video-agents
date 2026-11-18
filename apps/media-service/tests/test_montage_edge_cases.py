"""Cas limites du montage (#34) : unicode arabe, plan unique, 0 plan, double exécution."""

import shutil
import subprocess
from pathlib import Path

import pytest

from media_service.montage import MontageError, MontageJob, Plan, build_srt, parse_job, run_montage

from .test_montage import assets, make_job, probe  # noqa: F401

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe absents",
)

DARIJA = "مرحبا بيكم، كيفاش حالكم؟"


def test_arabic_darija_narration_is_written_as_utf8_and_montage_succeeds(
    assets: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    job = make_job(assets, tmp_path / "out.mp4")
    plans = (
        Plan(job.plans[0].clip_path, job.plans[0].audio_path, DARIJA),
        Plan(job.plans[1].clip_path, job.plans[1].audio_path, "Café déjà vu 🐱"),
    )
    result = run_montage(MontageJob(plans=plans, output_path=job.output_path))

    lines = result.srt_path.read_text(encoding="utf-8").split("\n")
    assert lines[2] == DARIJA
    assert "Café déjà vu 🐱" in lines
    assert float(probe(result.video_path)["format"]["duration"]) == pytest.approx(6.0, abs=0.2)


def test_multiline_narration_in_a_two_plan_job_keeps_cue_structure(
    assets: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    job = make_job(assets, tmp_path / "out.mp4")
    plans = (
        Plan(job.plans[0].clip_path, job.plans[0].audio_path, "Un\r\n\r\nDeux\n\n\nTrois"),
        Plan(job.plans[1].clip_path, job.plans[1].audio_path, "مرحبا\nبيكم"),
    )
    result = run_montage(MontageJob(plans=plans, output_path=job.output_path))

    blocks = result.srt_path.read_text(encoding="utf-8").strip().split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].split("\n")[2] == "Un Deux Trois"
    assert blocks[1].split("\n")[2] == "مرحبا بيكم"


def test_single_plan_lasts_as_long_as_its_voice(assets: Path, tmp_path: Path) -> None:  # noqa: F811
    plan = make_job(assets, tmp_path / "out.mp4").plans[0]
    result = run_montage(MontageJob(plans=(plan,), output_path=tmp_path / "out.mp4"))

    info = probe(result.video_path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert float(info["format"]["duration"]) == pytest.approx(3.0, abs=0.2)
    assert result.srt_path.read_text(encoding="utf-8").count("-->") == 1


def test_zero_plans_is_refused_by_parser(tmp_path: Path) -> None:
    with pytest.raises(MontageError, match="plans"):
        parse_job({"schema_version": "1.0", "plans": [], "output_path": str(tmp_path / "o.mp4")})


def test_zero_plans_job_is_refused_by_run_montage_and_leaves_no_file(tmp_path: Path) -> None:
    with pytest.raises((MontageError, ValueError, subprocess.SubprocessError)):
        run_montage(MontageJob(plans=(), output_path=tmp_path / "out.mp4"))
    assert list(tmp_path.iterdir()) == []


def test_build_srt_empty_inputs_gives_empty_text() -> None:
    assert build_srt([], []) == ""


def test_empty_or_blank_narration_is_refused_by_parser() -> None:
    plan = {"clip_path": "/c.mp4", "audio_path": "/a.m4a", "narration_fr": "  \n "}
    with pytest.raises(MontageError, match="plan 1"):
        parse_job({"schema_version": "1.0", "plans": [plan], "output_path": "/o.mp4"})


def test_rerun_overwrites_output_without_leftovers(assets: Path, tmp_path: Path) -> None:  # noqa: F811
    job = make_job(assets, tmp_path / "out.mp4")
    run_montage(job)
    run_montage(job)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.mp4", "out.srt"]
