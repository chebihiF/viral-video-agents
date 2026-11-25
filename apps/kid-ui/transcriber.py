"""Transcription de la dictée (#161) : interface locale, sans service tiers.

L'audio reste en mémoire : il n'est ni écrit sur disque ni journalisé, ni conservé après l'appel.
L'ASR réel est `FasterWhisperTranscriber` (#217) : faster-whisper local sur CPU, import paresseux.
"""

from __future__ import annotations

import importlib
import io
import threading
from pathlib import Path
from typing import Any, Protocol

MAX_AUDIO_BYTES = 1024 * 1024  # ~30 s d'audio compressé
ALLOWED_MIMES = frozenset({"audio/webm", "audio/ogg", "audio/mp4"})
DEFAULT_ASR_MODEL = Path.home() / ".cache" / "vva" / "asr" / "whisper-small-darija-ct2"
ASR_LANGUAGE = "ar"  # le darija n'a pas de code Whisper : arabe, le modèle affiné fait le reste


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, mime: str) -> str:
        """Texte dicté ; `mime` est le type de base (sans paramètres), déjà validé."""
        ...


class MockTranscriber:
    """Renvoie un texte fixe ; ne retient pas l'audio, seulement le nombre d'appels."""

    def __init__(self, text: str = "une vidéo de dragon") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, audio: bytes, mime: str) -> str:
        self.calls += 1
        return self.text


class FasterWhisperTranscriber:
    """ASR local faster-whisper (CPU, int8) ; extra optionnel `asr`, import paresseux.

    L'audio est décodé en mémoire (`BytesIO`) : jamais écrit sur disque, jamais journalisé.
    Le modèle (format CTranslate2) est lu uniquement depuis un dossier local : aucun téléchargement
    ni appel réseau au lancement ni à la dictée.
    """

    def __init__(
        self,
        model_path: str | Path = DEFAULT_ASR_MODEL,
        *,
        language: str = ASR_LANGUAGE,
        cpu_threads: int = 0,
        beam_size: int = 5,
    ) -> None:
        self.model_path = str(model_path)
        self.language = language
        self.cpu_threads = cpu_threads  # 0 : choix de CTranslate2
        self.beam_size = beam_size
        self._model: Any = None
        self._lock = threading.Lock()  # une transcription à la fois (CPU partagé)

    def load(self) -> None:
        """Charge le modèle (idempotent) ; ImportError si faster-whisper n'est pas installé."""
        with self._lock:
            self._load()

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            module = importlib.import_module("faster_whisper")
        except ImportError as exc:
            raise ImportError(
                "faster-whisper n'est pas installé : `uv sync --extra asr` "
                "(voir apps/kid-ui/README.md)"
            ) from exc
        self._model = module.WhisperModel(
            self.model_path,
            device="cpu",
            compute_type="int8",
            cpu_threads=self.cpu_threads,
            local_files_only=True,
        )

    def transcribe(self, audio: bytes, mime: str) -> str:
        with self._lock:
            self._load()
            # Le conteneur (webm/ogg/mp4) est détecté par PyAV sur le flux ; `mime` est inutile.
            segments, _info = self._model.transcribe(
                io.BytesIO(audio),
                language=self.language,
                task="transcribe",
                beam_size=self.beam_size,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            return " ".join(t for t in (seg.text.strip() for seg in segments) if t)
