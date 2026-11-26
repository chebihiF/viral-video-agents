"""Synthèse par réplique avec SILMA TTS (ADR-006) : un WAV et sa durée par réplique.

Contrat : contracts/tts-job.schema.json v2.0. Stdlib uniquement : le moteur tourne dans son propre
environnement Python derrière l'enveloppe `scripts/silma_cli.py` (SILMA_BIN). Liste d'arguments,
jamais de shell. Le texte et la transcription de la référence passent par stdin (JSON) : ni dans
les arguments, ni dans les logs, ni dans les erreurs (qui nomment la réplique ou la voix). Sorties
du moteur jamais relayées ; mode hors ligne forcé ; délai borné ; tout ou rien. Le moteur est
arrêté avec tout ce qu'il a lancé avant le retour (le GPU est libre pour l'étape suivante).
"""

import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "2.0"
DELAI_S: float = 600  # par réplique, chargement du modèle compris (un processus par réplique)
TAILLE_VOIX = 1_000_000  # octets lus au plus dans SILMA_VOIX
TAILLE_TRANSCRIPTION = 65_536
HORS_LIGNE = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}
# Environnement du moteur en liste blanche : rien d'autre n'est hérité (jetons, configuration).
ENV_TRANSMIS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"})
PREFIXES_TRANSMIS = ("CUDA_",)
SUFFIXES_TRANSMIS = ("_OFFLINE",)
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LANGUE = re.compile(r"[a-z]{2,3}")
_CLES = {"replique_id", "voix_id", "texte", "langue"}


class TtsError(Exception):
    """Synthèse impossible ; le message nomme la réplique, la voix ou la variable en cause."""


@dataclass(frozen=True)
class Replique:
    replique_id: str
    voix_id: str
    texte: str
    langue: str


@dataclass(frozen=True)
class TtsJob:
    output_dir: Path
    repliques: tuple[Replique, ...]


@dataclass(frozen=True)
class RepliqueVoix:
    replique_id: str
    wav_path: Path
    duree_s: float


@dataclass(frozen=True)
class Voix:
    """Référence d'une voix (hors dépôt) : audio de moins de 8 s et fichier de sa transcription."""

    audio: Path
    transcription: Path


@dataclass(frozen=True)
class Configuration:
    """Rendue par `verifier_configuration` : chemins absolus, voix indexées par voix_id."""

    binaire: str
    modeles: str
    voix: Mapping[str, Voix]


def _exact_keys(data: Any, keys: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or set(data) != keys:
        raise TtsError(f"{where} : objet avec exactement les clés {sorted(keys)} attendu")
    return data


def _replique(raw: Any, n: int) -> Replique:
    if isinstance(raw, Replique):  # issue de parse_job : revalidée comme un dict
        raw = {k: getattr(raw, k) for k in _CLES}
    item = _exact_keys(raw, _CLES, f"réplique {n}")
    rid, voix_id = item["replique_id"], item["voix_id"]
    if not isinstance(rid, str) or not _ID.fullmatch(rid):
        raise TtsError(f"réplique {n} : replique_id de 1 à 64 caractères [A-Za-z0-9_-] attendu")
    if not isinstance(voix_id, str) or not _ID.fullmatch(voix_id):
        raise TtsError(f"réplique {rid} : voix_id de 1 à 64 caractères [A-Za-z0-9_-] attendu")
    texte, langue = item["texte"], item["langue"]
    if not isinstance(texte, str) or not texte.strip() or "\0" in texte:
        raise TtsError(f"réplique {rid} : texte vide ou invalide")
    if not isinstance(langue, str) or not _LANGUE.fullmatch(langue):
        raise TtsError(f"réplique {rid} : langue invalide (code de 2 à 3 lettres minuscules)")
    return Replique(rid, voix_id, texte, langue)


def _repliques(raw: Any) -> tuple[Replique, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or not raw:
        raise TtsError("repliques : liste non vide attendue")
    repliques = tuple(_replique(item, n) for n, item in enumerate(raw, start=1))
    vus: set[str] = set()
    for r in repliques:
        if r.replique_id in vus:
            raise TtsError(f"réplique {r.replique_id} : replique_id en double")
        vus.add(r.replique_id)
    return repliques


def parse_job(data: Any) -> TtsJob:
    """Valide un tts-job (contrat v2.0) ; refuse tout écart, dont un job v1.0."""
    root = _exact_keys(data, {"schema_version", "output_dir", "repliques"}, "tts-job")
    if root["schema_version"] != SCHEMA_VERSION:
        raise TtsError(f"schema_version {root['schema_version']!r} non supportée")
    out = root["output_dir"]
    if not isinstance(out, str) or not out.strip() or "\0" in out:
        raise TtsError("output_dir : chaîne non vide sans caractère nul attendue")
    return TtsJob(output_dir=Path(out), repliques=_repliques(root["repliques"]))


def verifier_configuration(env: Mapping[str, str]) -> Configuration:
    """API publique : résout SILMA_BIN, SILMA_MODEL_DIR et SILMA_VOIX (voir README).

    Lève TtsError si une variable manque ou désigne un fichier introuvable ; le message nomme la
    variable (et la voix), jamais un chemin. Aucune voix par défaut, aucun repli sur Piper.
    """
    binaire = shutil.which(_variable(env, "SILMA_BIN", "indiquer l'enveloppe silma_cli"))
    if binaire is None:
        raise TtsError("SILMA_BIN introuvable ou non exécutable")
    modeles = Path(_variable(env, "SILMA_MODEL_DIR", "indiquer le dossier des poids locaux"))
    if not modeles.is_dir():
        raise TtsError("SILMA_MODEL_DIR introuvable : dossier des poids absent")
    voix = _voix(Path(_variable(env, "SILMA_VOIX", "indiquer le fichier JSON des voix")))
    return Configuration(os.path.abspath(binaire), os.path.abspath(modeles), voix)


def _variable(env: Mapping[str, str], nom: str, aide: str) -> str:
    valeur = env.get(nom, "").strip()
    if not valeur:
        raise TtsError(f"{nom} non configuré : {aide}")
    return valeur


def _voix(fichier: Path) -> dict[str, Voix]:
    """`{voix_id: {"audio": ..., "transcription": ...}}` ; chemins relatifs au fichier."""
    try:
        with fichier.open("rb") as f:
            brut = f.read(TAILLE_VOIX + 1)
        data = json.loads(brut) if len(brut) <= TAILLE_VOIX else None
    except (OSError, ValueError):
        data = None
        if not fichier.is_file():
            raise TtsError("SILMA_VOIX introuvable : fichier des voix absent") from None
    if not isinstance(data, dict) or not data:
        raise TtsError("SILMA_VOIX hors format : objet JSON {voix_id: référence} non vide attendu")
    return {voix_id: _reference(voix_id, ref, fichier.parent) for voix_id, ref in data.items()}


def _reference(voix_id: str, ref: Any, base: Path) -> Voix:
    if not _ID.fullmatch(voix_id):
        raise TtsError("SILMA_VOIX : identifiant de voix invalide ([A-Za-z0-9_-], 1 à 64)")
    item = _exact_keys(ref, {"audio", "transcription"}, f"SILMA_VOIX : voix {voix_id}")
    chemins = {}
    for cle in ("audio", "transcription"):
        valeur = item[cle]
        if not isinstance(valeur, str) or not valeur.strip() or "\0" in valeur:
            raise TtsError(f"SILMA_VOIX : voix {voix_id} : {cle} invalide")
        chemin = Path(os.path.abspath(base / valeur))
        if not chemin.is_file():
            raise TtsError(f"SILMA_VOIX : voix {voix_id} : fichier {cle} introuvable")
        chemins[cle] = chemin
    return Voix(chemins["audio"], chemins["transcription"])


def wav_duration(path: Path) -> float:
    """Durée en secondes d'un WAV PCM (module wave)."""
    try:
        with wave.open(str(path), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
    except (wave.Error, EOFError, OSError) as exc:
        raise TtsError("WAV illisible") from exc
    duration = frames / rate if rate else 0.0
    if not math.isfinite(duration) or duration <= 0:
        raise TtsError("WAV vide ou durée invalide")
    return duration


def _transcriptions(repliques: Sequence[Replique], config: Configuration) -> dict[str, str]:
    """Transcription de chaque voix utilisée, lue avant tout lancement du moteur."""
    lues: dict[str, str] = {}
    for r in repliques:
        if r.voix_id not in config.voix:
            raise TtsError(f"réplique {r.replique_id} : voix_id {r.voix_id} non configuré")
        if r.voix_id not in lues:
            lues[r.voix_id] = _transcription(r.voix_id, config.voix[r.voix_id].transcription)
    return lues


def _transcription(voix_id: str, chemin: Path) -> str:
    try:
        with chemin.open("rb") as f:
            texte = f.read(TAILLE_TRANSCRIPTION + 1).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        texte = ""
    if not texte.strip() or "\0" in texte or len(texte) > TAILLE_TRANSCRIPTION:
        raise TtsError(f"voix {voix_id} : transcription vide, illisible ou trop longue")
    return texte.strip()


def _lancer(cmd: list[str], entree: bytes, quoi: str) -> int:
    """Lance le moteur dans son propre groupe ; l'arrête (groupe compris) avant de rendre."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - liste d'arguments, pas de shell
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,  # sorties jamais relayées : elles peuvent citer le texte
            stderr=subprocess.DEVNULL,
            env=environnement_moteur(),
            start_new_session=True,
        )
    except OSError as exc:
        raise TtsError(f"{quoi} : moteur non exécutable ({exc.strerror})") from None
    try:
        proc.communicate(entree, timeout=DELAI_S)
    except subprocess.TimeoutExpired:
        depasse = True
    else:
        depasse = False
    finally:
        _arreter(proc)
    if depasse:
        raise TtsError(f"{quoi} : délai de {DELAI_S:g} s dépassé")
    return proc.returncode


def environnement_moteur() -> dict[str, str]:
    """Variables transmises au moteur : liste blanche, puis mode hors ligne forcé."""
    garde = {
        nom: valeur
        for nom, valeur in os.environ.items()
        if nom in ENV_TRANSMIS
        or nom.startswith(PREFIXES_TRANSMIS)
        or nom.endswith(SUFFIXES_TRANSMIS)
    }
    return {**garde, **HORS_LIGNE}


def _arreter(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _synthesize(
    r: Replique, ref: Voix, transcription: str, config: Configuration, out: Path
) -> RepliqueVoix:
    quoi = f"réplique {r.replique_id}"
    final = out / f"{r.replique_id}.wav"
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{r.replique_id}.", suffix=".wav", dir=out)
    except OSError as exc:
        raise TtsError(f"{quoi} : écriture impossible ({exc.strerror})") from None
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        cmd = [config.binaire, "--modeles", config.modeles, "--reference", str(ref.audio)]
        entree = json.dumps({"texte": r.texte, "transcription": transcription}).encode("utf-8")
        code = _lancer([*cmd, "--sortie", str(tmp)], entree, quoi)
        if code != 0:
            raise TtsError(f"{quoi} : le moteur a échoué (code {code})")
        try:
            duree = wav_duration(tmp)
        except TtsError as exc:
            raise TtsError(f"{quoi} : sortie du moteur invalide ({exc})") from None
        try:
            os.replace(tmp, final)
        except OSError as exc:
            raise TtsError(f"{quoi} : écriture impossible ({exc.strerror})") from None
        return RepliqueVoix(r.replique_id, final, duree)
    finally:
        tmp.unlink(missing_ok=True)


def synthetiser(
    repliques: Sequence[Mapping[str, Any] | Replique],
    out_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> list[RepliqueVoix]:
    """Un WAV `<replique_id>.wav` par réplique dans out_dir et sa durée ; tout ou rien.

    Entrée : liste de `{replique_id, voix_id, texte, langue}` ou de `Replique` (de `parse_job`).
    Configuration : SILMA_BIN, SILMA_MODEL_DIR, SILMA_VOIX (dans `env`, sinon l'environnement).
    Voix inconnue ou référence illisible : échec avant tout lancement du moteur.
    """
    parsed = _repliques(repliques)
    config = verifier_configuration(os.environ if env is None else env)
    transcriptions = _transcriptions(parsed, config)
    if not out_dir.is_dir():
        raise TtsError("dossier de sortie introuvable")
    voix: list[RepliqueVoix] = []
    try:
        for r in parsed:
            ref = config.voix[r.voix_id]
            voix.append(_synthesize(r, ref, transcriptions[r.voix_id], config, out_dir))
    except BaseException:  # tout ou rien, quelle que soit la cause (y compris Ctrl-C)
        for v in voix:
            v.wav_path.unlink(missing_ok=True)
        raise
    return voix
