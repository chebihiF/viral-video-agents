#!/usr/bin/env python3
"""Enveloppe côté moteur de SILMA TTS (#249, ADR-006) : une réplique, texte sur stdin, WAV.

    silma_cli.py --modeles DIR --reference AUDIO --sortie OUT.wav
    stdin : {"texte": "...", "transcription": "..."} (UTF-8)

À lancer avec le Python de l'environnement où `silma-tts` est installé (`SILMA_BIN` du
media-service) ; `silma_tts` n'est importé qu'à l'exécution. Hors ligne strict : variables
`*_OFFLINE`, connexions IPv4/IPv6 et résolution DNS refusées dans le processus, fichiers locaux
vérifiés avant l'import (un fichier manquant donne une erreur, jamais un téléchargement).
Le texte n'est jamais dans les arguments ni les messages : stdout et stderr de la bibliothèque
vont à /dev/null ; une erreur ne donne que son type. Aucun processus lancé par l'enveloppe.
Codes : 0 ok, 2 entrée invalide, 3 fichier local manquant, 4 réseau refusé, 5 échec du moteur.
Non validée sur le vrai moteur à la livraison (#249) : à valider au ticket d'installation.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import socket
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

# Disposition de --modeles (poids SILMA, vocodeur Vocos, tashkeel CATT), voir ADR-006.
MODELE = "silma-tts/model.pt"
VOCAB = "silma-tts/vocab.txt"
VOCODEUR = "vocos-mel-24khz"
CATT = ("catt/encoder.onnx", "catt/decoder.onnx")
FICHIERS = (MODELE, VOCAB, f"{VOCODEUR}/config.yaml", f"{VOCODEUR}/pytorch_model.bin", *CATT)
HF_SILMA = "hf://silma-ai/silma-tts/"
HORS_LIGNE = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")
TAILLE_ENTREE = 1_000_000
EX_ENTREE, EX_FICHIER, EX_RESEAU, EX_MOTEUR = 2, 3, 4, 5


class ReseauInterdit(OSError):
    """Connexion réseau tentée par la bibliothèque : refusée (mode hors ligne)."""


class _Garde:
    tentatives = 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    entree = _entree(sys.stdin.buffer.read(TAILLE_ENTREE + 1))
    if entree is None:
        return _erreur(EX_ENTREE, "entrée invalide : JSON {texte, transcription} non vides attendu")
    modeles = Path(args.modeles)
    manquants = [nom for nom in FICHIERS if not (modeles / nom).is_file()]
    if not Path(args.reference).is_file():
        return _erreur(EX_FICHIER, "audio de référence introuvable")
    if manquants:
        return _erreur(EX_FICHIER, f"fichier local manquant sous --modeles : {manquants[0]}")
    _hors_ligne()
    try:
        with _muet():
            _synthetiser(modeles, args.reference, entree, args.sortie)
    except (
        BaseException
    ) as exc:  # message réduit au type : celui de l'exception peut citer le texte
        Path(args.sortie).unlink(missing_ok=True)
        if _Garde.tentatives:
            return _erreur(EX_RESEAU, "connexion réseau refusée (mode hors ligne)")
        return _erreur(EX_MOTEUR, f"échec du moteur ({type(exc).__name__})")
    return 0


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SILMA TTS : une réplique (texte sur stdin)")
    parser.add_argument("--modeles", required=True, help="dossier des poids locaux")
    parser.add_argument("--reference", required=True, help="audio de la voix de référence")
    parser.add_argument("--sortie", required=True, help="WAV à écrire")
    return parser.parse_args(argv)


def _entree(brut: bytes) -> dict[str, str] | None:
    try:
        data = json.loads(brut.decode("utf-8")) if len(brut) <= TAILLE_ENTREE else None
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"texte", "transcription"}:
        return None
    if not all(isinstance(v, str) and v.strip() for v in data.values()):
        return None
    return {k: str(v) for k, v in data.items()}


def _erreur(code: int, message: str) -> int:
    print(f"silma_cli : {message}", file=sys.stderr)
    return code


def _hors_ligne() -> None:
    """Variables hors ligne et garde réseau, avant tout import de la bibliothèque."""
    for nom in HORS_LIGNE:
        os.environ[nom] = "1"
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def refuser(*_: Any, **__: Any) -> Any:
        _Garde.tentatives += 1
        raise ReseauInterdit("réseau interdit")

    def garder(original: Callable[..., Any]) -> Callable[..., Any]:
        def appel(sock: socket.socket, adresse: Any) -> Any:
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                return refuser()
            return original(sock, adresse)

        return appel

    setattr(socket.socket, "connect", garder(connect))  # noqa: B010
    setattr(socket.socket, "connect_ex", garder(connect_ex))  # noqa: B010
    socket.getaddrinfo = refuser
    socket.create_connection = refuser


@contextlib.contextmanager
def _muet() -> Iterator[None]:
    """stdout et stderr (descripteurs 1 et 2) vers /dev/null : la bibliothèque affiche le texte."""
    sys.stdout.flush()
    sys.stderr.flush()
    sauves = [os.dup(1), os.dup(2)]
    nul = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(nul, 1)
        os.dup2(nul, 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(sauves[0], 1)
        os.dup2(sauves[1], 2)
        for fd in (nul, *sauves):
            os.close(fd)


def _synthetiser(modeles: Path, reference: str, entree: dict[str, str], sortie: str) -> None:
    api = importlib.import_module("silma_tts.api")
    utils = importlib.import_module("silma_tts.infer.utils_infer")
    catt = importlib.import_module("catt_tashkeel")
    setattr(api, "cached_path", _local(modeles))  # noqa: B010 - poids lus sous --modeles
    encodeur, decodeur = (str(modeles / nom) for nom in CATT)
    tashkeel = catt.CATTEncoderOnly(encoder_path=encodeur, decoder_path=decodeur)
    setattr(utils, "tashkeel_model", tashkeel)  # noqa: B010 - sinon CATT télécharge ses poids
    moteur = api.SilmaTTS(vocoder_local_path=str(modeles / VOCODEUR))
    moteur.infer(
        ref_file=reference,
        ref_text=entree["transcription"],  # fournie : pas de transcription automatique (Whisper)
        gen_text=entree["texte"],
        file_wave=sortie,
    )


def _local(modeles: Path) -> Callable[..., str]:
    def resoudre(url: str, cache_dir: Any = None) -> str:
        nom = url.removeprefix(HF_SILMA) if url.startswith(HF_SILMA) else ""
        chemin = modeles / "silma-tts" / nom
        if not nom or "/" in nom or not chemin.is_file():
            raise FileNotFoundError("fichier de modèle hors de --modeles")
        return str(chemin)

    return resoudre


if __name__ == "__main__":
    sys.exit(main())
