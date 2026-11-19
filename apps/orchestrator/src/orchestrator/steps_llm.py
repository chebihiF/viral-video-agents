"""Étapes LLM réelles : classification et storyboard via Ollama, échec fermé (#137).

Prompts versionnés (`prompts/`) et sorties validées contre `contracts/` par un validateur stdlib
(sous-ensemble de JSON Schema ; mot-clé ou type inconnu = erreur de configuration). Sortie
invalide : `SortieNonConforme`, sans nouvel essai (#143 : temperature 0 et seed fixe rejoueraient
la même réponse). Serveur injoignable, redirection : `OllamaInjoignable` ; proxy d'environnement
ignoré. Jamais de classification par défaut. Aucune dictée ni sortie du modèle dans les logs ou les
exceptions (levées hors de tout bloc `except` : ni `__cause__` ni `__context__`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.pipeline import Job, SortieNonConforme

log = logging.getLogger(__name__)

RACINE = Path(__file__).resolve().parents[4]
MODELE = "qwen3:8b"
SEED = 42
TAILLE_MAX = 1_000_000  # octets de réponse lus au plus
TIMEOUT_S = 300.0
_URL = re.compile(r"https?://[^\s/?#]+(/\S*)?")
_CHEVRONS = str.maketrans("<>", "‹›")
_BLOC_TEXT = re.compile(r"```text\n(.*?)\n```", re.DOTALL)
_FRONT_MATTER = re.compile(r"---\n.*?\n---\n(.*)", re.DOTALL)
_HORS_FORMAT = ("$schema", "$id", "title", "description", "allOf")  # vérifiés après coup

Transport = Callable[[str, bytes, float], bytes]  # (url, corps JSON, timeout) -> réponse


class ErreurConfiguration(Exception):
    """OLLAMA_URL absente ou invalide, prompt ou schéma illisible."""


class OllamaInjoignable(Exception):
    """Ollama n'a pas répondu (message sans contenu de la requête)."""


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    """Toute redirection est refusée : la réponse 3xx remonte en HTTPError (échec fermé)."""

    def redirect_request(self, *_: Any) -> None:
        return None


# Ni proxy d'environnement (HTTP(S)_PROXY) ni redirection : la requête ne va qu'à OLLAMA_URL.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SansRedirection())


def _post_urllib(url: str, corps: bytes, timeout: float) -> bytes:
    # S310 : schéma http(s) imposé par ClientOllama.__post_init__.
    requete = urllib.request.Request(url, corps, {"Content-Type": "application/json"})  # noqa: S310
    with _OPENER.open(requete, timeout=timeout) as reponse:
        brut: bytes = reponse.read(TAILLE_MAX + 1)
    return brut


@dataclass(frozen=True)
class ClientOllama:
    """Client `/api/chat` d'Ollama ; `transport` injectable (faux client en test)."""

    url: str
    modele: str = MODELE
    transport: Transport = _post_urllib
    timeout_s: float = TIMEOUT_S

    def __post_init__(self) -> None:
        if not _URL.fullmatch(self.url):
            raise ErreurConfiguration("OLLAMA_URL doit être une URL http(s)")

    def discuter(self, systeme: str, utilisateur: str, format_: Mapping[str, Any]) -> str | None:
        """Contenu de la réponse du modèle ; None si l'enveloppe est illisible."""
        corps = {
            "model": self.modele,
            "stream": False,
            "think": False,
            "format": format_,
            "options": {"temperature": 0, "seed": SEED},
            "messages": [
                {"role": "system", "content": systeme},
                {"role": "user", "content": utilisateur},
            ],
        }
        brut = self._envoyer(json.dumps(corps).encode())
        return _contenu(brut) if len(brut) <= TAILLE_MAX else None

    def _envoyer(self, corps: bytes) -> bytes:
        try:
            return self.transport(self.url.rstrip("/") + "/api/chat", corps, self.timeout_s)
        except Exception as exc:  # toute panne de transport : échec fermé
            nom = type(exc).__name__
        log.warning("ollama injoignable type=%s", nom)
        raise OllamaInjoignable("ollama injoignable")


def client_depuis_env(
    env: Mapping[str, str] | None = None, transport: Transport = _post_urllib
) -> ClientOllama:
    """Client configuré par OLLAMA_URL (racine du serveur, ex. http://hote:11434)."""
    url = (os.environ if env is None else env).get("OLLAMA_URL", "").strip()
    if not url:
        raise ErreurConfiguration("OLLAMA_URL non configurée")
    return ClientOllama(url, transport=transport)


@dataclass(frozen=True)
class _Consigne:
    nom: str
    systeme: str
    schema: Mapping[str, Any]


def classifier_ollama(client: ClientOllama, racine: Path = RACINE) -> Callable[[str], Job]:
    """Étape `classifier` : dictée -> classification.schema.json (prompt classification.v1)."""
    texte = _lire(racine / "prompts" / "classification.v1.md")
    bloc = _BLOC_TEXT.search(texte)
    if not bloc:
        raise ErreurConfiguration("prompt de classification sans bloc text")
    consigne = _Consigne(
        "classification",
        bloc.group(1),
        charger_schema(racine / "contracts" / "classification.schema.json"),
    )
    return lambda dictee: _demander(client, consigne, encadrer(dictee))


def storyboard_ollama(client: ClientOllama, racine: Path = RACINE) -> Callable[[str], Job]:
    """Étape `storyboard` : texte autorisé -> storyboard.schema.json (prompt storyboard.v1).

    Le texte (dictée ou `texte_adouci` produit par le modèle) est cadré comme la dictée (#147).
    """
    corps = _FRONT_MATTER.fullmatch(_lire(racine / "prompts" / "storyboard.v1.md"))
    if not corps:
        raise ErreurConfiguration("prompt de storyboard sans en-tête")
    consigne = _Consigne(
        "storyboard",
        corps.group(1).strip(),
        charger_schema(racine / "contracts" / "storyboard.schema.json"),
    )
    return lambda texte: _demander(client, consigne, encadrer(texte))


def encadrer(dictee: str) -> str:
    """La dictée est une donnée : balisée, normalisée NFKC, sans aucun `<` ni `>` (‹ ›).

    Même règle que `wrap_dictee` d'`evals/classification_eval.py` (script stdlib autonome).
    """
    return "<dictee>" + unicodedata.normalize("NFKC", dictee).translate(_CHEVRONS) + "</dictee>"


def _demander(client: ClientOllama, consigne: _Consigne, utilisateur: str) -> Job:
    format_ = {k: v for k, v in consigne.schema.items() if k not in _HORS_FORMAT}
    sortie = _json(client.discuter(consigne.systeme, utilisateur, format_))
    if isinstance(sortie, dict) and conforme(sortie, consigne.schema, consigne.schema):
        return sortie
    log.warning("etape=%s sortie non conforme", consigne.nom)
    raise SortieNonConforme(f"{consigne.nom} : sortie non conforme")


def _contenu(brut: bytes) -> str | None:
    enveloppe = _json(brut.decode("utf-8", errors="replace"))
    message = enveloppe.get("message") if isinstance(enveloppe, dict) else None
    contenu = message.get("content") if isinstance(message, dict) else None
    return contenu if isinstance(contenu, str) else None


def _json(texte: str | None) -> Any:
    """Objet JSON strict (NaN et Infinity refusés) ; None si illisible ou trop imbriqué."""
    if texte is None:
        return None
    try:
        return json.loads(texte, parse_constant=_refuser)
    except (ValueError, RecursionError):
        return None


def _refuser(_: str) -> Any:
    raise ValueError("constante JSON non standard")


def _lire(chemin: Path) -> str:
    try:
        return chemin.read_text(encoding="utf-8")
    except OSError:
        pass
    raise ErreurConfiguration(f"fichier illisible : {chemin.name}")


# --- Validateur : sous-ensemble de JSON Schema 2020-12 utilisé par les contrats ---

_ANNOTATIONS = frozenset({"$schema", "$id", "title", "description", "default", "$defs"})
_SOUS_SCHEMA = frozenset({"items", "not", "if", "then"})
_LISTES = frozenset({"allOf", "anyOf"})
_DICTS = frozenset({"properties", "$defs"})
_VALEURS = frozenset({"type", "const", "enum", "minLength", "maxLength", "required", "$ref"})
_BORNES = frozenset({"minItems", "maxItems", "exclusiveMinimum", "additionalProperties"})
_MOTS_CLES = _ANNOTATIONS | _SOUS_SCHEMA | _LISTES | _DICTS | _VALEURS | _BORNES


def charger_schema(chemin: Path) -> Mapping[str, Any]:
    """Schéma JSON du contrat ; mot-clé non géré par le validateur = erreur de configuration."""
    try:
        schema = json.loads(_lire(chemin))
    except ValueError:
        schema = None
    if not isinstance(schema, dict):
        raise ErreurConfiguration(f"schéma illisible : {chemin.name}")
    _verifier_mots_cles(schema)
    return schema


def _verifier_mots_cles(schema: Any) -> None:
    if not isinstance(schema, dict) or not set(schema) <= _MOTS_CLES:
        raise ErreurConfiguration("schéma : mot-clé non géré par le validateur")
    if schema.get("additionalProperties", False) is not False:
        raise ErreurConfiguration("schéma : seul additionalProperties=false est géré")
    if "type" in schema and (not isinstance(schema["type"], str) or schema["type"] not in _TYPES):
        raise ErreurConfiguration("schéma : type non géré par le validateur")
    enfants = [schema[k] for k in _SOUS_SCHEMA & set(schema)]
    enfants += [s for k in _LISTES & set(schema) for s in schema[k]]
    enfants += [s for k in _DICTS & set(schema) for s in schema[k].values()]
    for enfant in enfants:
        _verifier_mots_cles(enfant)


def _nombre(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "number": _nombre,
}


def conforme(valeur: Any, schema: Mapping[str, Any], racine: Mapping[str, Any]) -> bool:
    """Vrai si `valeur` respecte `schema` (références `#/...` résolues dans `racine`)."""
    if "$ref" in schema:
        reste = {k: v for k, v in schema.items() if k != "$ref"}
        return conforme(valeur, _cible(schema["$ref"], racine), racine) and conforme(
            valeur, reste, racine
        )
    regles = (_type_valeur, _chaine, _borne, _objet, _tableau, _logique)
    return all(regle(valeur, schema, racine) for regle in regles)


def _cible(ref: str, racine: Mapping[str, Any]) -> Mapping[str, Any]:
    noeud: Any = racine if ref.startswith("#/") else None
    for partie in ref[2:].split("/"):
        noeud = noeud.get(partie) if isinstance(noeud, dict) else None
    if not isinstance(noeud, dict):
        raise ErreurConfiguration("schéma : $ref introuvable")
    return noeud


def _egal(a: Any, b: Any) -> bool:
    return ((_nombre(a) and _nombre(b)) or type(a) is type(b)) and a == b


def _type_valeur(v: Any, s: Mapping[str, Any], _: Mapping[str, Any]) -> bool:
    if "type" in s and not _TYPES[s["type"]](v):
        return False
    if "const" in s and not _egal(v, s["const"]):
        return False
    return "enum" not in s or any(_egal(v, e) for e in s["enum"])


def _chaine(v: Any, s: Mapping[str, Any], _: Mapping[str, Any]) -> bool:
    if not isinstance(v, str):
        return True
    return bool(s.get("minLength", 0) <= len(v) <= s.get("maxLength", len(v)))


def _borne(v: Any, s: Mapping[str, Any], _: Mapping[str, Any]) -> bool:
    return not _nombre(v) or "exclusiveMinimum" not in s or bool(v > s["exclusiveMinimum"])


def _objet(v: Any, s: Mapping[str, Any], r: Mapping[str, Any]) -> bool:
    if not isinstance(v, dict):
        return True
    props = s.get("properties", {})
    if not all(k in v for k in s.get("required", [])):
        return False
    if s.get("additionalProperties") is False and not set(v) <= set(props):
        return False
    return all(conforme(v[k], props[k], r) for k in v if k in props)


def _tableau(v: Any, s: Mapping[str, Any], r: Mapping[str, Any]) -> bool:
    if not isinstance(v, list):
        return True
    if not s.get("minItems", 0) <= len(v) <= s.get("maxItems", len(v)):
        return False
    return "items" not in s or all(conforme(e, s["items"], r) for e in v)


def _logique(v: Any, s: Mapping[str, Any], r: Mapping[str, Any]) -> bool:
    if not all(conforme(v, sous, r) for sous in s.get("allOf", [])):
        return False
    if "anyOf" in s and not any(conforme(v, sous, r) for sous in s["anyOf"]):
        return False
    if "not" in s and conforme(v, s["not"], r):
        return False
    return "if" not in s or not conforme(v, s["if"], r) or conforme(v, s.get("then", {}), r)
