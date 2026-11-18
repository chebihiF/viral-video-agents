#!/usr/bin/env python3
"""Éval du prompt storyboard : taux de sorties JSON conformes à contracts/storyboard.schema.json.

Mode réel : Ollama (Qwen3 8B), température 0, seed fixe, une requête à la fois (une seule étape GPU).
Mode --dry-run : rejoue evals/cases/storyboard.recorded.jsonl, aucun accès réseau, avec le schéma
de test (evals/cases/storyboard.test.schema.json) ; code 2 si les enregistrements ont été faits
avec un autre schéma (empreinte sha256).
Code de sortie : 0 si taux >= seuil, 1 sinon (liste des cas en échec), 2 si erreur de configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "cases" / "storyboard.jsonl"
RECORDED = ROOT / "evals" / "cases" / "storyboard.recorded.jsonl"
CONTRACT = ROOT / "contracts" / "storyboard.schema.json"
TEST_SCHEMA = ROOT / "evals" / "cases" / "storyboard.test.schema.json"
PROMPT = ROOT / "prompts" / "storyboard.v1.md"

OLLAMA_URL = "http://127.0.0.1:11434"
MODEL = "qwen3:8b"
SEED = 42
THRESHOLD = 0.90

_ANNOTATIONS = {"$schema", "$id", "$comment", "title", "description", "default", "examples"}
_SUPPORTED = {
    "type",
    "required",
    "properties",
    "items",
    "enum",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "additionalProperties",
}


class ConfigError(Exception):
    """Erreur de configuration (schéma absent, mot-clé non géré, fichier illisible)."""


_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    py = {
        "string": str,
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }.get(expected)
    if py is None:
        raise ConfigError(f"type JSON Schema inconnu : {expected!r}")
    return isinstance(value, py)


def check_schema(schema: Any, path: str = "$") -> None:
    """Parcours unique du schéma : ConfigError sur tout mot-clé ou forme non gérés."""
    if not isinstance(schema, dict):
        raise ConfigError(f"sous-schéma invalide (objet attendu) à {path}")
    unknown = set(schema) - _SUPPORTED - _ANNOTATIONS
    if unknown:
        raise ConfigError(f"mots-clés de schéma non gérés à {path} : {sorted(unknown)}")
    types = schema.get("type", [])
    for t in types if isinstance(types, list) else [types]:
        if t not in _TYPES:
            raise ConfigError(f"type JSON Schema inconnu à {path} : {t!r}")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise ConfigError(f"additionalProperties doit être un booléen à {path}")
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            raise ConfigError(f"properties doit être un objet à {path}")
        for name, sub in schema["properties"].items():
            check_schema(sub, f"{path}.{name}")
    if "items" in schema:
        check_schema(schema["items"], f"{path}[]")


def schema_fingerprint(schema: dict[str, Any]) -> str:
    canon = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Valide `value` contre un sous-ensemble de JSON Schema ; renvoie la liste des erreurs."""
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, t) for t in types):
            return [f"{path}: type {types} attendu, reçu {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: valeur hors enum {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: longueur < {schema['minLength']}")
        if len(value) > schema.get("maxLength", len(value)):
            errors.append(f"{path}: longueur > {schema['maxLength']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: {len(value)} éléments < minItems {schema['minItems']}")
        if len(value) > schema.get("maxItems", len(value)):
            errors.append(f"{path}: {len(value)} éléments > maxItems {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: champ requis absent : {name}")
        for name, sub in props.items():
            if name in value:
                errors += validate(value[name], sub, f"{path}.{name}")
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: champ inattendu : {n}" for n in value if n not in props]
    return errors


def load_schema(dry_run: bool, explicit: Path | None) -> tuple[dict[str, Any], str]:
    """Réel : le contrat. Dry-run : le schéma de test fourni avec les enregistrements, jamais le
    vrai contrat (sauf --schema explicite)."""
    if explicit is not None:
        path = explicit
    elif dry_run:
        path = TEST_SCHEMA
    elif CONTRACT.exists():
        path = CONTRACT
    else:
        raise ConfigError(
            f"schéma absent : {CONTRACT} (ticket #31 non mergé). "
            "L'éval réelle exige le vrai contrat ; --dry-run utilise un schéma de test."
        )
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"schéma illisible : {path} ({exc})") from exc
    check_schema(schema)
    return schema, str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError) as exc:
        raise ConfigError(f"fichier illisible : {path} ({exc})") from exc


def load_system_prompt(path: Path = PROMPT) -> str:
    """Corps du prompt, sans l'en-tête `---` (version, modèle cible)."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        _, _, text = text[4:].partition("\n---\n")
    return text.strip()


def ask_ollama(
    dictee: str, system: str, schema: dict[str, Any], url: str, model: str, seed: int
) -> str:
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "format": schema,
        "options": {"temperature": 0, "seed": seed},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": dictee}],
    }
    req = urllib.request.Request(
        f"{url}/api/chat",
        json.dumps(payload).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        content = json.loads(resp.read())["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("contenu non textuel")
    return content


def check_response(raw: str | None, schema: dict[str, Any]) -> list[str]:
    if raw is None:
        return ["aucune réponse"]
    if not isinstance(raw, str):
        return ["réponse non textuelle"]
    try:
        data = json.loads(raw)
    except ValueError:
        return ["JSON invalide"]
    return validate(data, schema)


def run(
    cases: list[dict[str, Any]],
    responder: Any,
    schema: dict[str, Any],
    threshold: float = THRESHOLD,
) -> dict[str, Any]:
    """Rapport déterministe : aucune durée ni date, pour comparer deux exécutions."""
    failures = []
    for case in cases:
        try:
            raw = responder(case)
            errors = check_response(raw, schema)
        except OSError as exc:  # réseau, timeout
            errors = [f"appel échoué : {exc}"]
        except (KeyError, ValueError, TypeError) as exc:  # réponse Ollama mal formée
            errors = [f"réponse Ollama inexploitable : {exc!r}"]
        if errors:
            failures.append({"id": case["id"], "erreurs": errors})
    total = len(cases)
    ok = total - len(failures)
    rate = ok / total if total else 0.0
    return {
        "total": total,
        "conformes": ok,
        "taux": round(rate, 4),
        "seuil": threshold,
        "succes": total > 0 and rate >= threshold,
        "echecs": failures,
    }


def format_report(report: dict[str, Any], meta: dict[str, Any]) -> str:
    lines = [
        f"storyboard_eval  mode={meta['mode']}  modele={meta['model']}  seed={meta['seed']}",
        f"prompt={meta['prompt']}  schema={meta['schema']}",
        f"conformes {report['conformes']}/{report['total']}  taux={report['taux']:.1%}  "
        f"seuil={report['seuil']:.0%}  -> {'OK' if report['succes'] else 'ECHEC'}",
    ]
    for fail in report["echecs"]:
        lines.append(f"  - {fail['id']}: " + " ; ".join(fail["erreurs"][:3]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    ap.add_argument("--dry-run", action="store_true", help="rejoue les réponses enregistrées")
    ap.add_argument("--cases", type=Path, default=CASES)
    ap.add_argument("--recorded", type=Path, default=RECORDED)
    ap.add_argument("--schema", type=Path, default=None)
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--ollama-url", default=OLLAMA_URL)
    ap.add_argument("--record", type=Path, default=None, help="(réel) écrit les réponses brutes")
    args = ap.parse_args(argv)
    if not 0.0 <= args.threshold <= 1.0:
        print("erreur de configuration : --threshold doit être dans [0, 1]", file=sys.stderr)
        return 2

    try:
        schema, schema_name = load_schema(args.dry_run, args.schema)
        cases = read_jsonl(args.cases)
        if args.dry_run:
            fingerprint = schema_fingerprint(schema)
            entries = read_jsonl(args.recorded)
            stale = sorted({str(e.get("schema_sha256")) for e in entries} - {fingerprint})
            if stale:
                raise ConfigError(
                    f"enregistrements faits avec un autre schéma ({', '.join(stale)} != "
                    f"{fingerprint}) : ré-enregistrer avec --record"
                )
            recorded = {e["id"]: e["response"] for e in entries}

            def responder(case: dict[str, Any]) -> str | None:
                return recorded.get(case["id"])
        else:
            system = load_system_prompt()
            captured: list[dict[str, str]] = []

            def responder(case: dict[str, Any]) -> str | None:
                t0 = time.time()
                raw = ask_ollama(
                    case["dictee"], system, schema, args.ollama_url, args.model, args.seed
                )
                print(f"[{case['id']}] {time.time() - t0:.1f}s", file=sys.stderr)
                captured.append(
                    {"id": case["id"], "schema_sha256": schema_fingerprint(schema), "response": raw}
                )
                return raw

        report = run(cases, responder, schema, args.threshold)
        if not args.dry_run and args.record:
            args.record.write_text(
                "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in captured),
                encoding="utf-8",
            )
    except (ConfigError, KeyError, TypeError, AttributeError) as exc:
        print(f"erreur de configuration : {exc}", file=sys.stderr)
        return 2

    meta = {
        "mode": "dry-run" if args.dry_run else "ollama",
        "model": args.model,
        "seed": args.seed,
        "prompt": PROMPT.name,
        "schema": schema_name,
    }
    print(format_report(report, meta))
    return 0 if report["succes"] else 1


if __name__ == "__main__":
    sys.exit(main())
