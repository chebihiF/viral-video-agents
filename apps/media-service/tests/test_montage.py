"""Montage FFmpeg (#34) : clips et voix synthétiques générés par le test (lavfi)."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from media_service.montage import (
    MontageError,
    MontageJob,
    Plan,
    build_srt,
    escape_filter_value,
    format_srt_time,
    probe_duration,
    run_montage,
)

pytestmark = pytest.mark.skipif(
    (shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None)
    and not os.environ.get("CI"),  # en CI l'absence de ffmpeg doit faire échouer, pas ignorer
    reason="ffmpeg/ffprobe absents",
)

NARRATIONS = ("Le petit chat s'envole !", "Bonjour les oiseaux.")


def _ffmpeg(*args: str) -> None:
    subprocess.run(  # noqa: S603
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


def make_clip(path: Path, seconds: int = 4) -> Path:
    src = f"testsrc=size=320x240:rate=30:duration={seconds}"
    _ffmpeg("-f", "lavfi", "-i", src, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    return path


def make_voice(path: Path, seconds: int = 3) -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", str(path))
    return path


def probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,codec_type,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    data: dict[str, Any] = json.loads(out)
    return data


@pytest.fixture(scope="module")
def assets(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("assets")
    for i in (1, 2):
        make_clip(d / f"clip{i}.mp4")
        make_voice(d / f"voice{i}.m4a")
    make_clip(d / "short.mp4", seconds=1)
    return d


def make_job(assets: Path, output: Path, clips: tuple[str, str] = ("clip1", "clip2")) -> MontageJob:
    plans = tuple(
        Plan(assets / f"{c}.mp4", assets / f"voice{i}.m4a", NARRATIONS[i - 1])
        for i, c in enumerate(clips, start=1)
    )
    return MontageJob(plans=plans, output_path=output)


def test_two_plans_give_1080x1920_6_seconds_h264_aac_24fps(assets: Path, tmp_path: Path) -> None:
    result = run_montage(make_job(assets, tmp_path / "out.mp4"))

    info = probe(result.video_path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264"
    assert video["r_frame_rate"] == "24/1"
    assert audio["codec_name"] == "aac"
    assert float(info["format"]["duration"]) == pytest.approx(6.0, abs=0.2)


def test_srt_has_both_narrations_in_order_with_voice_timings(assets: Path, tmp_path: Path) -> None:
    result = run_montage(make_job(assets, tmp_path / "out.mp4"))

    assert result.srt_path == tmp_path / "out.srt"
    blocks = result.srt_path.read_text(encoding="utf-8").strip().split("\n\n")
    assert len(blocks) == 2
    first, second = (b.split("\n") for b in blocks)
    assert first[0] == "1"
    assert first[2] == NARRATIONS[0]
    assert second[0] == "2"
    assert second[2] == NARRATIONS[1]
    start1, end1 = first[1].split(" --> ")
    start2, _ = second[1].split(" --> ")
    assert start1 == "00:00:00,000"
    assert end1 == start2
    assert 2.8 <= _seconds(end1) <= 3.2


def _seconds(srt_time: str) -> float:
    hms, ms = srt_time.split(",")
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s + int(ms) / 1000


def test_plan_lasts_as_long_as_its_voice_even_if_clip_is_shorter(
    assets: Path, tmp_path: Path
) -> None:
    job = make_job(assets, tmp_path / "out.mp4", clips=("short", "clip2"))
    result = run_montage(job)
    assert float(probe(result.video_path)["format"]["duration"]) == pytest.approx(6.0, abs=0.2)


def test_output_dir_with_filter_special_characters(assets: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "a b,c'd:e[1];f=g"
    out_dir.mkdir()
    result = run_montage(make_job(assets, out_dir / "out.mp4"))
    assert result.video_path.is_file()


def test_paths_starting_with_dash_are_not_taken_for_options(
    assets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copy(assets / "clip1.mp4", tmp_path / "-clip1.mp4")
    shutil.copy(assets / "voice1.m4a", tmp_path / "-voice1.m4a")
    shutil.copy(assets / "clip2.mp4", tmp_path / "-clip2.mp4")
    shutil.copy(assets / "voice2.m4a", tmp_path / "-voice2.m4a")
    monkeypatch.chdir(tmp_path)
    job = MontageJob(
        plans=(
            Plan(Path("-clip1.mp4"), Path("-voice1.m4a"), NARRATIONS[0]),
            Plan(Path("-clip2.mp4"), Path("-voice2.m4a"), NARRATIONS[1]),
        ),
        output_path=Path("-out.mp4"),
    )
    assert run_montage(job).video_path.is_file()


def test_missing_clip_names_the_plan_and_leaves_no_file(assets: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    job = make_job(assets, out_dir / "out.mp4")
    missing = assets / "absent.mp4"
    job = MontageJob(
        plans=(job.plans[0], Plan(missing, job.plans[1].audio_path, NARRATIONS[1])),
        output_path=job.output_path,
    )

    with pytest.raises(MontageError, match=r"plan 2.*clip.*absent\.mp4"):
        run_montage(job)
    assert list(out_dir.iterdir()) == []


def test_missing_audio_names_the_plan_and_leaves_no_file(assets: Path, tmp_path: Path) -> None:
    job = make_job(assets, tmp_path / "out.mp4")
    job = MontageJob(
        plans=(Plan(job.plans[0].clip_path, assets / "nope.m4a", NARRATIONS[0]), job.plans[1]),
        output_path=job.output_path,
    )
    with pytest.raises(MontageError, match=r"plan 1.*audio.*nope\.m4a"):
        run_montage(job)
    assert list(tmp_path.iterdir()) == []


def test_ffmpeg_failure_leaves_no_partial_file_and_keeps_previous_output(
    assets: Path, tmp_path: Path
) -> None:
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"not a video")
    previous = tmp_path / "out" / "out.mp4"
    previous.parent.mkdir()
    previous.write_bytes(b"previous")
    job = make_job(assets, previous)
    job = MontageJob(
        plans=(Plan(corrupt, job.plans[0].audio_path, NARRATIONS[0]), job.plans[1]),
        output_path=previous,
    )

    with pytest.raises(MontageError, match="ffmpeg"):
        run_montage(job)
    assert [p.name for p in previous.parent.iterdir()] == ["out.mp4"]
    assert previous.read_bytes() == b"previous"


def test_missing_output_directory_is_refused(assets: Path, tmp_path: Path) -> None:
    with pytest.raises(MontageError, match="dossier"):
        run_montage(make_job(assets, tmp_path / "nope" / "out.mp4"))


def test_srt_collapses_newlines_in_narration(assets: Path, tmp_path: Path) -> None:
    job = make_job(assets, tmp_path / "out.mp4")
    plans = (Plan(job.plans[0].clip_path, job.plans[0].audio_path, "Ligne 1\n\nLigne 2"),)
    result = run_montage(MontageJob(plans=plans, output_path=job.output_path))
    assert result.srt_path.read_text(encoding="utf-8").split("\n")[2] == "Ligne 1 Ligne 2"


def test_output_path_that_is_a_directory_is_refused(assets: Path, tmp_path: Path) -> None:
    (tmp_path / "out.mp4").mkdir()
    with pytest.raises(MontageError, match=r"out\.mp4.*dossier"):
        run_montage(make_job(assets, tmp_path / "out.mp4"))
    assert [p.name for p in tmp_path.iterdir()] == ["out.mp4"]


def test_srt_target_that_is_a_directory_is_refused_before_any_work(
    assets: Path, tmp_path: Path
) -> None:
    (tmp_path / "out.srt").mkdir()
    with pytest.raises(MontageError, match=r"out\.srt.*dossier"):
        run_montage(make_job(assets, tmp_path / "out.mp4"))
    assert [p.name for p in tmp_path.iterdir()] == ["out.srt"]


def test_failure_installing_the_srt_restores_previous_pair(
    assets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video, srt = tmp_path / "out.mp4", tmp_path / "out.srt"
    video.write_bytes(b"old video")
    srt.write_text("old srt")
    real_replace = os.replace

    def failing_replace(src: Any, dst: Any) -> None:
        if Path(dst) == srt:
            raise OSError("disque plein")
        real_replace(src, dst)

    monkeypatch.setattr("media_service.montage.os.replace", failing_replace)
    with pytest.raises(MontageError, match="disque plein"):
        run_montage(make_job(assets, video))
    assert video.read_bytes() == b"old video"
    assert srt.read_text() == "old srt"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.mp4", "out.srt"]


def test_failure_installing_the_srt_without_previous_output_leaves_nothing(
    assets: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace

    def failing_replace(src: Any, dst: Any) -> None:
        if Path(dst).suffix == ".srt":
            raise OSError("boom")
        real_replace(src, dst)

    monkeypatch.setattr("media_service.montage.os.replace", failing_replace)
    with pytest.raises(MontageError):
        run_montage(make_job(assets, tmp_path / "out.mp4"))
    assert list(tmp_path.iterdir()) == []


def test_srt_neutralises_ass_and_html_markup() -> None:
    text = build_srt(["{\\an8}<b>Salut</b> {x}"], [1.0])
    assert text.split("\n")[2] == "(\\an8)\u2039b\u203aSalut\u2039/b\u203a (x)"
    assert not any(c in text.split("\n")[2] for c in "{}<>")


def test_probe_failure_reports_stderr_tail(tmp_path: Path) -> None:
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"garbage")
    with pytest.raises(MontageError, match=r"ffprobe.*bad\.m4a.*\S"):
        probe_duration(bad)


def test_format_srt_time() -> None:
    assert format_srt_time(0) == "00:00:00,000"
    assert format_srt_time(3723.4567) == "01:02:03,457"


def test_escape_filter_value_escapes_both_levels() -> None:
    assert escape_filter_value("/a b/c.srt") == "/a b/c.srt"
    assert escape_filter_value("a:b") == r"a\\:b"
    assert escape_filter_value("a,b[1];c'd") == r"a\,b\[1\]\;c\\\'d"
