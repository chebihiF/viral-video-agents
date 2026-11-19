"""Étapes voix (Piper) et montage réelles (#136) : faux Piper, lavfi, sans fuite de texte."""

from __future__ import annotations

import json
import logging
import shutil
import stat
import subprocess
import sys
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from orchestrator.pipeline import Etapes, lancer
from orchestrator.steps_llm import ErreurConfiguration
from orchestrator.steps_media import montage_reel, voix_piper

RACINE = Path(__file__).resolve().parents[3]
CONTRACTS = RACINE / "contracts"
SECRET = "Secret-Prenom-Enfant"  # noqa: S105 - marqueur de fuite

FAKE_PIPER = f"""#!{sys.executable}
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

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe absents",
)


def _schema(nom: str) -> Any:
    return json.loads((CONTRACTS / nom).read_text(encoding="utf-8"))


def _tts_job(out: Path) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "output_dir": str(out),
        "plans": [
            {"plan_id": "plan-1", "texte": f"{SECRET} dit un deux trois", "langue": "fr"},
            {"plan_id": "plan-2", "texte": "quatre cinq six sept", "langue": "fr"},
        ],
    }


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    binary = tmp_path / "piper"
    binary.write_text(FAKE_PIPER, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    model = tmp_path / "voix.onnx"
    model.write_bytes(b"model")
    return {"PIPER_BIN": str(binary), "PIPER_MODEL": str(model)}


def test_voix_reelle_avec_faux_piper(env: dict[str, str], tmp_path: Path) -> None:
    out = tmp_path / "run" / "voix"  # le dossier est créé par l'étape
    resultat = voix_piper(env)(_tts_job(out))

    resolver = Draft202012Validator(_schema("tts-job.schema.json"))
    schema = {"$ref": "#/$defs/TtsResult", "$defs": resolver.schema["$defs"]}
    Draft202012Validator(schema).validate(resultat)
    assert [p["plan_id"] for p in resultat["plans"]] == ["plan-1", "plan-2"]
    assert sorted(p.name for p in out.glob("*.wav")) == ["plan-1.wav", "plan-2.wav"]
    for p in resultat["plans"]:
        assert Path(p["wav_path"]).is_file()
        assert p["duree_s"] > 0


@pytest.mark.parametrize("env_vide", [{}, {"PIPER_BIN": "piper-inexistant-xyz"}])
def test_piper_absent_erreur_de_configuration(env_vide: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(ErreurConfiguration, match="PIPER_BIN"):
        voix_piper(env_vide)(_tts_job(tmp_path / "voix"))


def test_modele_absent_erreur_de_configuration(env: dict[str, str], tmp_path: Path) -> None:
    del env["PIPER_MODEL"]
    with pytest.raises(ErreurConfiguration, match="PIPER_MODEL"):
        voix_piper(env)(_tts_job(tmp_path / "voix"))


def test_piper_absent_range_le_run_en_erreur_sur_voix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from orchestrator.mocks import etapes_factices

    base = etapes_factices()
    etapes = Etapes(
        base.classifier,
        base.storyboard,
        voix_piper({}),
        base.image,
        base.clip,
        base.montage,
    )
    with caplog.at_level(logging.DEBUG):
        run = lancer("Dictée de test", etapes, run_id="r1", workdir=str(tmp_path))
    assert run["etat"] == "erreur"
    assert [(e["nom"], e["statut"]) for e in run["etapes"]][-1] == ("voix", "erreur")
    assert "ErreurConfiguration" in caplog.text


def test_tts_job_hors_contrat_sans_fuite(env: dict[str, str], tmp_path: Path) -> None:
    job = _tts_job(tmp_path / "voix")
    job["plans"][0]["texte"] = SECRET + "\0"
    with pytest.raises(Exception) as err:  # noqa: B017, PT011
        voix_piper(env)(job)
    assert SECRET not in str(err.value)
    assert err.value.__cause__ is None and err.value.__context__ is None


# --- montage -----------------------------------------------------------------------------------


def _ffmpeg(*args: str) -> None:
    subprocess.run(  # noqa: S603
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


def _wav(path: Path, secondes: float) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\0\0" * int(24000 * secondes))
    return path


def _job_montage(tmp_path: Path, narration: str = "Le petit chat s'envole !") -> dict[str, Any]:
    plans = []
    for i, texte in enumerate((narration, "Bonjour les oiseaux."), start=1):
        clip = tmp_path / f"clip{i}.mp4"
        src = "testsrc=size=320x240:rate=30:duration=2"
        _ffmpeg("-f", "lavfi", "-i", src, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip))
        plans.append(
            {
                "clip_path": str(clip),
                "audio_path": str(_wav(tmp_path / f"voix{i}.wav", 1.5)),
                "narration_fr": texte,
            }
        )
    sortie = tmp_path / "run" / "final.mp4"
    return {"schema_version": "1.0", "plans": plans, "output_path": str(sortie)}


@needs_ffmpeg
def test_montage_ecrit_l_ass_a_cote_du_mp4(tmp_path: Path) -> None:
    job = _job_montage(tmp_path)
    Draft202012Validator(_schema("montage-job.schema.json")).validate(job)

    video = montage_reel()(job)

    assert video == job["output_path"]
    mp4 = Path(video)
    ass = mp4.with_suffix(".ass")
    assert mp4.is_file() and ass.is_file()
    contenu = ass.read_text(encoding="utf-8")
    assert "Dialogue:" in contenu and "chat" in contenu
    info = json.loads(
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                video,
            ],  # fmt: skip
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (1080, 1920)


def test_montage_passe_subtitles_path_a_run_montage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from media_service import montage as module
    from media_service.montage import MontageResult

    vus: list[Any] = []

    def faux(job: Any) -> MontageResult:
        vus.append(job)
        job.output_path.write_bytes(b"mp4")
        return MontageResult(job.output_path, job.output_path.with_suffix(".srt"))

    monkeypatch.setattr("orchestrator.steps_media.run_montage", faux)
    monkeypatch.setattr("orchestrator.steps_media.probe_duration", lambda _p: 1.5)
    assert module  # même module que celui réellement utilisé
    job = {
        "schema_version": "1.0",
        "plans": [{"clip_path": "c.mp4", "audio_path": "a.wav", "narration_fr": "un deux"}],
        "output_path": str(tmp_path / "run" / "final.mp4"),
    }
    montage_reel()(job)
    assert vus[0].subtitles_path == (tmp_path / "run" / "final.ass")
    assert vus[0].subtitles_path.is_file()


def test_echec_montage_sans_fuite_de_narration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from media_service.montage import MontageError

    def echoue(_job: Any) -> Any:
        raise MontageError(f"ffmpeg a échoué : Parsed_subtitles {SECRET}")

    monkeypatch.setattr("orchestrator.steps_media.run_montage", echoue)
    monkeypatch.setattr("orchestrator.steps_media.probe_duration", lambda _p: 1.5)
    job: Mapping[str, Any] = {
        "schema_version": "1.0",
        "plans": [{"clip_path": "c.mp4", "audio_path": "a.wav", "narration_fr": f"{SECRET} salue"}],
        "output_path": str(tmp_path / "run" / "final.mp4"),
    }
    with caplog.at_level(logging.DEBUG), pytest.raises(Exception) as err:  # noqa: B017, PT011
        montage_reel()(job)
    assert SECRET not in str(err.value) and SECRET not in caplog.text
    assert err.value.__cause__ is None and err.value.__context__ is None
    assert not (tmp_path / "run" / "final.ass").exists()  # pas d'ASS orphelin


def test_montage_job_hors_contrat_sans_fuite(tmp_path: Path) -> None:
    job = {"schema_version": "2", "plans": [], "output_path": "x.mp4", "narration": SECRET}
    with pytest.raises(Exception) as err:  # noqa: B017, PT011
        montage_reel()(job)
    assert SECRET not in str(err.value)
