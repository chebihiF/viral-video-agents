"""ASR local faster-whisper (#217) : faux module partout, jamais le vrai ni un modèle."""

from __future__ import annotations

import io
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import server
import transcriber
from transcriber import FasterWhisperTranscriber, MockTranscriber

AUDIO = b"\x1aE\xdf\xa3-audio-d-enfant-secret"


class Seg:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModel:
    inits: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def __init__(self, path: str, **kw: Any) -> None:
        FakeModel.inits.append({"path": path, **kw})

    def transcribe(self, stream: Any, **kw: Any) -> tuple[Any, Any]:
        FakeModel.calls.append({"stream": stream, "data": stream.getvalue(), **kw})
        return iter([Seg(" salam "), Seg(""), Seg("dragon ")]), object()


@pytest.fixture
def fake_module(monkeypatch: pytest.MonkeyPatch) -> type[FakeModel]:
    FakeModel.inits, FakeModel.calls = [], []
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return FakeModel


def test_import_paresseux_construction_sans_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import impossible
    tr = FasterWhisperTranscriber("/modele")  # ne doit pas importer faster_whisper
    with pytest.raises(ImportError, match="uv sync --extra asr"):
        tr.transcribe(AUDIO, "audio/webm")


def test_transcription_en_memoire_cpu_int8(fake_module: type[FakeModel]) -> None:
    tr = FasterWhisperTranscriber("/modele")
    assert tr.transcribe(AUDIO, "audio/webm") == "salam dragon"
    init, call = fake_module.inits[0], fake_module.calls[0]
    assert init["path"] == "/modele"
    assert (init["device"], init["compute_type"], init["local_files_only"]) == ("cpu", "int8", True)
    assert isinstance(call["stream"], io.BytesIO)  # jamais un chemin de fichier
    assert call["data"] == AUDIO
    assert call["language"] == "ar"
    assert call["condition_on_previous_text"] is False


def test_modele_charge_une_seule_fois(fake_module: type[FakeModel]) -> None:
    tr = FasterWhisperTranscriber("/modele")
    tr.transcribe(AUDIO, "audio/ogg")
    tr.transcribe(AUDIO, "audio/mp4")
    assert len(fake_module.inits) == 1
    assert len(fake_module.calls) == 2


def test_aucune_trace_ni_fichier(
    fake_module: type[FakeModel],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.DEBUG):
        FasterWhisperTranscriber("/modele").transcribe(AUDIO, "audio/webm")
    assert "secret" not in caplog.text
    assert "salam" not in caplog.text
    assert list(tmp_path.iterdir()) == []


def test_serveur_asr_valide_modele_et_exclusivite(tmp_path: Path) -> None:
    sans_modele = server.parse_args(
        ["--insecure-dev", "--asr", "faster-whisper", "--asr-model", str(tmp_path / "absent")]
    )
    assert "introuvable" in (server.validate(sans_modele) or "")
    ok = server.parse_args(
        ["--insecure-dev", "--asr", "faster-whisper", "--asr-model", str(tmp_path)]
    )
    assert server.validate(ok) is None
    both = server.parse_args(
        ["--insecure-dev", "--asr", "faster-whisper", "--mock-asr", "--asr-model", str(tmp_path)]
    )
    assert "exclusifs" in (server.validate(both) or "")


def test_serveur_choix_du_transcriber(fake_module: type[FakeModel], tmp_path: Path) -> None:
    assert server.make_transcriber(server.parse_args([])) is None  # défaut : 503
    assert isinstance(server.make_transcriber(server.parse_args(["--mock-asr"])), MockTranscriber)
    real = server.make_transcriber(
        server.parse_args(["--asr", "faster-whisper", "--asr-model", str(tmp_path)])
    )
    assert isinstance(real, FasterWhisperTranscriber)
    assert fake_module.inits[0]["path"] == str(tmp_path)  # chargé au démarrage


def test_serveur_sans_module_sort_proprement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    a = server.parse_args(["--asr", "faster-whisper", "--asr-model", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        server.make_transcriber(a)
    assert exc.value.code == 2
    assert "uv sync --extra asr" in capsys.readouterr().err


def test_modele_par_defaut_hors_depot() -> None:
    assert transcriber.DEFAULT_ASR_MODEL.is_relative_to(Path.home())
