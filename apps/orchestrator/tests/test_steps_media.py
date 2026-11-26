"""Étapes voix (SILMA, #249) et montage réelles (#136) : faux moteur, lavfi, sans fuite de texte."""

from __future__ import annotations

import ast
import json
import logging
import shutil
import subprocess
import wave
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fakes import FAUX_SILMA, VOIX_OFF, appels_faux_silma, env_faux_silma
from jsonschema import Draft202012Validator

from orchestrator.pipeline import Etapes, lancer
from orchestrator.steps_llm import ErreurConfiguration
from orchestrator.steps_media import ErreurVoix, montage_reel, voix_silma

RACINE = Path(__file__).resolve().parents[3]
CONTRACTS = RACINE / "contracts"
SECRET = "Secret-Prenom-Enfant"  # noqa: S105 - marqueur de fuite

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe absents",
)


def _schema(nom: str) -> Any:
    return json.loads((CONTRACTS / nom).read_text(encoding="utf-8"))


def _tts_job(out: Path, voix_id: str = VOIX_OFF) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "output_dir": str(out),
        "repliques": [
            {
                "replique_id": "r1",
                "voix_id": voix_id,
                "texte": f"{SECRET} dit un deux trois",
                "langue": "ar",
            },
            {
                "replique_id": "r2",
                "voix_id": VOIX_OFF,
                "texte": "quatre cinq six sept",
                "langue": "ar",
            },
        ],
    }


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return env_faux_silma(tmp_path / "silma")


def test_voix_reelle_avec_faux_silma(env: dict[str, str], tmp_path: Path) -> None:
    out = tmp_path / "run" / "voix"  # le dossier est créé par l'étape
    resultat = voix_silma(env)(_tts_job(out))

    schema = {"$ref": "#/$defs/TtsResult", "$defs": _schema("tts-job.schema.json")["$defs"]}
    Draft202012Validator(schema).validate(resultat)
    assert [r["replique_id"] for r in resultat["repliques"]] == ["r1", "r2"]
    assert sorted(p.name for p in out.glob("*.wav")) == ["r1.wav", "r2.wav"]
    assert [r["duree_s"] for r in resultat["repliques"]] == [
        pytest.approx(1.25),
        pytest.approx(1.0),
    ]
    assert len(appels_faux_silma(env)) == 2


@pytest.mark.parametrize("variable", ["SILMA_BIN", "SILMA_MODEL_DIR", "SILMA_VOIX"])
def test_configuration_absente_erreur_de_configuration(
    env: dict[str, str], tmp_path: Path, variable: str
) -> None:
    del env[variable]
    with pytest.raises(ErreurConfiguration, match=variable) as err:
        voix_silma(env)(_tts_job(tmp_path / "voix"))
    assert err.value.__cause__ is None and err.value.__context__ is None


def test_piper_seul_ne_suffit_plus(tmp_path: Path) -> None:
    with pytest.raises(ErreurConfiguration, match="SILMA_BIN"):
        voix_silma({"PIPER_BIN": "piper", "PIPER_MODEL": "voix.onnx"})(_tts_job(tmp_path))


def test_configuration_absente_range_le_run_en_erreur_sur_voix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from orchestrator.mocks import etapes_factices

    base = etapes_factices()
    etapes = Etapes(
        base.classifier,
        base.storyboard,
        voix_silma({}),
        base.image,
        base.clip,
        base.montage,
    )
    with caplog.at_level(logging.DEBUG):
        run = lancer("Dictée de test", etapes, run_id="r1", workdir=str(tmp_path))
    assert run["etat"] == "erreur"
    assert [(e["nom"], e["statut"]) for e in run["etapes"]][-1] == ("voix", "erreur")
    assert "ErreurConfiguration" in caplog.text


def test_voix_inconnue_aucun_moteur_lance(env: dict[str, str], tmp_path: Path) -> None:
    out = tmp_path / "voix"
    with pytest.raises(ErreurVoix, match="renard"):
        voix_silma(env)(_tts_job(out, voix_id="renard"))
    assert appels_faux_silma(env) == []
    assert list(out.glob("*.wav")) == []


def test_tts_job_hors_contrat_sans_fuite(env: dict[str, str], tmp_path: Path) -> None:
    job = _tts_job(tmp_path / "voix")
    job["repliques"][0]["texte"] = SECRET + "\0"
    with pytest.raises(ErreurVoix) as err:
        voix_silma(env)(job)
    assert SECRET not in str(err.value)
    assert err.value.__cause__ is None and err.value.__context__ is None


def test_job_1_0_refuse(env: dict[str, str], tmp_path: Path) -> None:
    job = {"schema_version": "1.0", "output_dir": str(tmp_path), "plans": []}
    with pytest.raises(ErreurVoix, match="tts-job"):
        voix_silma(env)(job)


def test_moteur_bavard_en_echec_sans_fuite(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bavard = (
        FAUX_SILMA
        + "sys.stdout.write(entree['texte'])\nsys.stderr.write(entree['texte'])\nsys.exit(4)\n"
    )
    env = env_faux_silma(tmp_path / "silma", bavard)
    with caplog.at_level(logging.DEBUG), pytest.raises(ErreurVoix, match="r1") as err:
        voix_silma(env)(_tts_job(tmp_path / "voix"))
    assert SECRET not in str(err.value) and SECRET not in caplog.text
    assert err.value.__cause__ is None and err.value.__context__ is None
    assert not [a for a in appels_faux_silma(env) if SECRET in json.dumps(a)]
    assert list((tmp_path / "voix").glob("*.wav")) == []


# Scénarios (#249): texte jamais exposé, échec au milieu des répliques (run complet)


MOTEUR_BAVARD_A_LA_2E = FAUX_SILMA + (
    "c = Path(sys.argv[0]).with_suffix('.n')\n"
    "n = int(c.read_text()) if c.exists() else 0\n"
    "c.write_text(str(n + 1))\n"
    "if n >= 1:\n"
    "    sys.stdout.write(entree['texte'])\n"
    "    sys.stderr.write(entree['texte'])\n"
    "    sys.exit(3)\n"
)


def test_run_en_erreur_sur_voix_sans_wav_ni_mp4_ni_texte(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from orchestrator.mocks import etapes_factices

    env = env_faux_silma(tmp_path / "silma", MOTEUR_BAVARD_A_LA_2E)
    base = etapes_factices()
    etapes = Etapes(
        base.classifier,
        base.storyboard,
        voix_silma(env),
        base.image,
        base.clip,
        base.montage,
        env["VOIX_OFF_ID"],
    )
    narrations = [p["narration_fr"] for p in base.storyboard("x")["plans"]]
    with caplog.at_level(logging.DEBUG):
        run = lancer("Dictée de test", etapes, run_id="r249", workdir=str(tmp_path / "w"))
    assert run["etat"] == "erreur"
    assert (run["etapes"][-1]["nom"], run["etapes"][-1]["statut"]) == ("voix", "erreur")
    assert "video_path" not in run
    assert list((tmp_path / "w").rglob("*.wav")) == []
    assert list((tmp_path / "w").rglob("*.mp4")) == []
    assert len(appels_faux_silma(env)) == 2
    for texte in (json.dumps(run), caplog.text, json.dumps(appels_faux_silma(env))):
        assert not [n for n in narrations if n in texte]
    fichiers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert not [p for p in fichiers for n in narrations if n.encode() in p.read_bytes()]


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


def test_steps_media_n_importe_aucun_nom_prive_de_tts() -> None:
    source = (RACINE / "apps/orchestrator/src/orchestrator/steps_media.py").read_text("utf-8")
    noms = [
        alias.name
        for noeud in ast.walk(ast.parse(source))
        if isinstance(noeud, ast.ImportFrom) and noeud.module == "media_service.tts"
        for alias in noeud.names
    ]
    assert noms and not [n for n in noms if n.startswith("_")]
