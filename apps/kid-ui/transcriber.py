"""Transcription de la dictée (#161) : interface locale, sans service tiers.

L'audio reste en mémoire : il n'est ni écrit sur disque ni journalisé, ni conservé après l'appel.
L'ASR réel (faster-whisper / whisper.cpp, local) viendra avec un ticket séparé.
"""

from __future__ import annotations

from typing import Protocol

MAX_AUDIO_BYTES = 1024 * 1024  # ~30 s d'audio compressé
ALLOWED_MIMES = frozenset({"audio/webm", "audio/ogg", "audio/mp4"})


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
