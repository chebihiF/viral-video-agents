"""Voix off par plan avec Piper (CPU) : un WAV et sa durée par plan.

Contrat : contracts/tts-job.schema.json. Stdlib uniquement. Piper est appelé avec une liste
d'arguments (jamais de shell) ; le texte passe par stdin, donc ni dans la ligne de commande,
ni dans les logs, ni dans les erreurs (qui nomment le plan, jamais son texte).
"""

import math
import os
import re
import shutil
import subprocess
import tempfile
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
PIPER_TIMEOUT_S = 120
_PLAN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LANGUE = re.compile(r"[a-z]{2,3}")


class TtsError(Exception):
    """Synthèse impossible ; le message nomme le plan ou la configuration en cause."""


@dataclass(frozen=True)
class Plan:
    plan_id: str
    texte: str
    langue: str


@dataclass(frozen=True)
class TtsJob:
    output_dir: Path
    plans: tuple[Plan, ...]


@dataclass(frozen=True)
class PlanVoice:
    plan_id: str
    wav_path: Path
    duree_s: float


def _exact_keys(data: Any, keys: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or set(data) != keys:
        raise TtsError(f"{where} : objet avec exactement les clés {sorted(keys)} attendu")
    return data


def _plan(raw: Any, n: int) -> Plan:
    if isinstance(raw, Plan):  # Plan issu de parse_job : revalidé comme un dict
        raw = {"plan_id": raw.plan_id, "texte": raw.texte, "langue": raw.langue}
    item = _exact_keys(raw, {"plan_id", "texte", "langue"}, f"plan {n}")
    plan_id = item["plan_id"]
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        raise TtsError(f"plan {n} : plan_id de 1 à 64 caractères [A-Za-z0-9_-] attendu")
    texte, langue = item["texte"], item["langue"]
    if not isinstance(texte, str) or not texte.strip() or "\0" in texte:
        raise TtsError(f"plan {plan_id} : texte vide ou invalide")
    if not isinstance(langue, str) or not _LANGUE.fullmatch(langue):
        raise TtsError(f"plan {plan_id} : langue invalide (code de 2 à 3 lettres minuscules)")
    return Plan(plan_id, texte, langue)


def _plans(raw_plans: Any) -> tuple[Plan, ...]:
    if not isinstance(raw_plans, Sequence) or isinstance(raw_plans, str) or not raw_plans:
        raise TtsError("plans : liste non vide attendue")
    plans = tuple(_plan(raw, n) for n, raw in enumerate(raw_plans, start=1))
    seen: set[str] = set()
    for plan in plans:
        if plan.plan_id in seen:
            raise TtsError(f"plan {plan.plan_id} : plan_id en double")
        seen.add(plan.plan_id)
    return plans


def parse_job(data: Any) -> TtsJob:
    """Valide un tts-job (contrat v1.0) ; refuse tout écart."""
    root = _exact_keys(data, {"schema_version", "output_dir", "plans"}, "tts-job")
    if root["schema_version"] != SCHEMA_VERSION:
        raise TtsError(f"schema_version {root['schema_version']!r} non supportée")
    out = root["output_dir"]
    if not isinstance(out, str) or not out.strip() or "\0" in out:
        raise TtsError("output_dir : chaîne non vide sans caractère nul attendue")
    return TtsJob(output_dir=Path(out), plans=_plans(root["plans"]))


def verifier_configuration(env: Mapping[str, str]) -> tuple[str, str]:
    """API publique : résout PIPER_BIN (exécutable) et PIPER_MODEL (fichier) en chemins absolus.

    Lève TtsError (message sans texte de dictée) si l'un manque ou est introuvable.
    """
    configured = env.get("PIPER_BIN", "").strip()
    if not configured:
        raise TtsError("PIPER_BIN non configuré : indiquer le binaire Piper")
    binary = shutil.which(configured)
    if binary is None:
        raise TtsError("PIPER_BIN introuvable ou non exécutable : installer Piper ou corriger")
    model = env.get("PIPER_MODEL", "").strip()
    if not model:
        raise TtsError("PIPER_MODEL non configuré : indiquer le modèle de voix (.onnx)")
    if not Path(model).is_file():
        raise TtsError("PIPER_MODEL introuvable : fichier du modèle de voix absent")
    return os.path.abspath(binary), os.path.abspath(model)


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


def _synthesize(plan: Plan, binary: str, model: str, out_dir: Path) -> PlanVoice:
    final = out_dir / f"{plan.plan_id}.wav"
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{plan.plan_id}.", suffix=".wav", dir=out_dir)
    except OSError as exc:
        raise TtsError(f"plan {plan.plan_id} : écriture impossible ({exc.strerror})") from exc
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        cmd = [binary, "--model", model, "--output_file", str(tmp)]
        try:
            done = subprocess.run(  # noqa: S603 - liste d'arguments, pas de shell
                cmd,
                input=plan.texte.encode("utf-8"),
                stdout=subprocess.DEVNULL,  # sorties jamais relayées : elles pourraient
                stderr=subprocess.DEVNULL,  # citer le texte
                timeout=PIPER_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TtsError(f"plan {plan.plan_id} : Piper a dépassé {PIPER_TIMEOUT_S} s") from exc
        except OSError as exc:
            raise TtsError(f"plan {plan.plan_id} : Piper non exécutable ({exc.strerror})") from exc
        if done.returncode != 0:
            raise TtsError(f"plan {plan.plan_id} : Piper a échoué (code {done.returncode})")
        try:
            duration = wav_duration(tmp)
        except TtsError as exc:
            raise TtsError(f"plan {plan.plan_id} : sortie Piper invalide ({exc})") from exc
        try:
            os.replace(tmp, final)
        except OSError as exc:
            raise TtsError(f"plan {plan.plan_id} : écriture impossible ({exc.strerror})") from exc
        return PlanVoice(plan.plan_id, final, duration)
    finally:
        tmp.unlink(missing_ok=True)


def synthetiser(
    plans: Sequence[Mapping[str, Any] | Plan],
    out_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> list[PlanVoice]:
    """Un WAV `<plan_id>.wav` par plan dans out_dir et sa durée ; tout ou rien.

    Entrée : liste de `{plan_id, texte, langue}` ou de `Plan` (issus de `parse_job`).
    Configuration : PIPER_BIN et PIPER_MODEL (dans `env`, sinon l'environnement du processus).
    """
    parsed = _plans(plans)
    binary, model = verifier_configuration(os.environ if env is None else env)
    if not out_dir.is_dir():
        raise TtsError("dossier de sortie introuvable")
    voices: list[PlanVoice] = []
    try:
        for plan in parsed:
            voices.append(_synthesize(plan, binary, model, out_dir))
    except BaseException:  # tout ou rien, quelle que soit la cause (y compris Ctrl-C)
        for voice in voices:
            voice.wav_path.unlink(missing_ok=True)
        raise
    return voices
