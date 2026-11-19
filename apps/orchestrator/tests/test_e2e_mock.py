"""Parcours de bout en bout dictée -> MP4 avec mocks (#139) : `make e2e-mock`.

CLI réel, faux Piper, clips lavfi, montage FFmpeg réel avec ASS. Aucun réseau, aucun GPU.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import wave
from pathlib import Path
from typing import Any

import pytest
from fakes import SECONDES_PAR_MOT, TraceGpu, env_faux_piper, etapes_e2e

from orchestrator import __main__ as cli
from orchestrator.mocks import MOT_BLOQUE, etapes_factices
from orchestrator.pipeline import Etapes, lancer
from orchestrator.steps_media import voix_piper

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe absents",
    ),
]

DICTEE = "Dictée neutre de test : un chat sur un tapis."  # fixe, aucune donnée d'enfant


def _cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, etapes: Etapes, dictee: str = DICTEE
) -> tuple[int, Path]:
    """Lance `orchestrator run` avec les étapes d'intégration injectées dans le CLI."""
    monkeypatch.setattr(cli, "etapes_factices", lambda: etapes)
    fichier = tmp_path / "dictee.txt"
    fichier.write_text(dictee, encoding="utf-8")
    out = tmp_path / "out"
    code = cli.main(["run", "--mocks", "--dictee-file", str(fichier), "--out", str(out)])
    return code, out


def _run(out: Path) -> dict[str, Any]:
    run: dict[str, Any] = json.loads((out / "run.json").read_text("utf-8"))
    return run


def _ffprobe(mp4: Path) -> dict[str, Any]:
    brut = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type,width,height:format=duration",
            "-of",
            "json",
            str(mp4),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    info: dict[str, Any] = json.loads(brut)
    return info


def _etapes(tmp_path: Path) -> Etapes:
    return etapes_e2e(tmp_path / "clips", env_piper=env_faux_piper(tmp_path))


def _narrations() -> list[str]:
    plans = etapes_factices().storyboard("x")["plans"]
    return [p["narration_fr"] for p in plans]


def _duree_wav(chemin: Path) -> float:
    with wave.open(str(chemin), "rb") as w:
        return w.getnframes() / w.getframerate()


def test_parcours_complet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out = _cli(tmp_path, monkeypatch, _etapes(tmp_path))

    run = _run(out)
    assert code == 0 and run["etat"] == "ok"
    assert [e["nom"] for e in run["etapes"]] == [
        "classification",
        "storyboard",
        "voix",
        "image",
        "clip",
        "montage",
    ]
    mp4 = Path(run["video_path"])
    assert mp4.is_file() and mp4.is_relative_to(out)
    info = _ffprobe(mp4)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 1080, 1920)
    assert audio["codec_name"] == "aac"
    voix = sorted((mp4.parent / "voix").glob("*.wav"))
    assert len(voix) == 2
    attendu = sum(_duree_wav(w) for w in voix)
    assert attendu == pytest.approx(
        sum(len(n.split()) for n in _narrations()) * SECONDES_PAR_MOT, abs=0.01
    )
    assert float(info["format"]["duration"]) == pytest.approx(attendu, abs=0.2)


def test_sous_titres_un_evenement_par_mot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out = _cli(tmp_path, monkeypatch, _etapes(tmp_path))

    mp4 = Path(_run(out)["video_path"])
    ass = mp4.with_suffix(".ass")
    assert ass.is_file()
    evenements = [
        ligne for ligne in ass.read_text("utf-8").splitlines() if ligne.startswith("Dialogue:")
    ]
    assert len(evenements) == sum(len(n.split()) for n in _narrations())


def test_contenu_bloque_sans_mp4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out = _cli(tmp_path, monkeypatch, _etapes(tmp_path), f"{DICTEE} {MOT_BLOQUE}")

    run = _run(out)
    assert code == 2 and run["etat"] == "bloque"
    assert [e["nom"] for e in run["etapes"]] == ["classification"]
    assert "video_path" not in run
    assert list(tmp_path.rglob("*.mp4")) == []
    assert list(tmp_path.rglob("*.wav")) == []


def test_piper_absent_erreur_sur_voix_sans_mp4_partiel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base = _etapes(tmp_path)
    sans_piper = Etapes(
        base.classifier, base.storyboard, voix_piper({}), base.image, base.clip, base.montage
    )
    with caplog.at_level(logging.DEBUG):
        code, out = _cli(tmp_path, monkeypatch, sans_piper)

    run = _run(out)
    assert code == 1 and run["etat"] == "erreur"
    assert (run["etapes"][-1]["nom"], run["etapes"][-1]["statut"]) == ("voix", "erreur")
    assert "video_path" not in run
    assert list(tmp_path.rglob("*.mp4")) == []
    assert list(tmp_path.rglob("*.ass")) == []
    assert DICTEE not in caplog.text


def test_aucune_dictee_dans_les_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        _cli(tmp_path, monkeypatch, _etapes(tmp_path))

    assert "chat sur un tapis" not in caplog.text
    assert "chat sur un tapis" not in (tmp_path / "out" / "run.json").read_text("utf-8")


def test_etapes_gpu_en_serie(tmp_path: Path) -> None:
    trace = TraceGpu()
    env = env_faux_piper(tmp_path)

    def montage_neutre(job: Any) -> str:
        return str(
            job["output_path"]
        )  # le montage réel est couvert ailleurs ; ici seul le GPU compte

    etapes = etapes_e2e(tmp_path / "clips", env_piper=env, trace=trace, montage=montage_neutre)
    runs: list[dict[str, Any]] = []

    def parcours(rid: str) -> None:
        runs.append(lancer(DICTEE, etapes, run_id=rid, workdir=str(tmp_path / "w")))

    fils = [threading.Thread(target=parcours, args=(f"r{n}",)) for n in (1, 2)]
    for f in fils:
        f.start()
    for f in fils:
        f.join(timeout=120)

    assert [r["etat"] for r in runs] == ["ok", "ok"]
    assert len(trace.intervalles) == 2 * 2 * 2  # 2 runs x 2 plans x (image + clip)
    assert trace.chevauchements() == []
