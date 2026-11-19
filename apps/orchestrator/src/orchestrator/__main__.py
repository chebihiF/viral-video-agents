"""CLI de l'orchestrateur (#138) : `python -m orchestrator run --mocks --dictee-file F --out DIR`.

Codes de sortie : 0 ok, 1 erreur, 2 bloque, 64 usage/dictée invalide, 78 configuration absente
(sans `--mocks` : OLLAMA_URL, PIPER_BIN, PIPER_MODEL ; échec fermé avant tout appel réseau).
Image et clip réels (gpu-worker) ne sont pas branchés : le run finit « erreur » (#150).
La dictée n'est ni écrite, ni journalisée, ni affichée.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from orchestrator.mocks import etapes_factices
from orchestrator.pipeline import Etapes, lancer, ligne_durees
from orchestrator.steps_llm import ErreurConfiguration

DICTEE_MAX = 2000
EX_USAGE = 64
EX_CONFIG = 78
_CODES = {"ok": 0, "erreur": 1, "bloque": 2}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EX_USAGE, f"orchestrator: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orchestrator", description="Orchestrateur du pipeline vidéo.")
    sub = parser.add_subparsers(dest="commande", required=True, parser_class=_Parser)
    run = sub.add_parser("run", help="lance un parcours depuis une dictée texte")
    run.add_argument("--dictee-file", required=True, help="fichier de dictée UTF-8, « - » = stdin")
    run.add_argument(
        "--out", required=True, type=Path, help="dossier de sortie (run.json et durees.jsonl)"
    )
    run.add_argument("--mocks", action="store_true", help="étapes factices (sans GPU)")
    return parser


def _lire_dictee(source: str) -> str | None:
    """Dictée lue puis validée ; None si illisible, vide ou trop longue."""
    try:
        if source == "-":
            texte = sys.stdin.read(DICTEE_MAX + 1)
        else:
            with open(source, encoding="utf-8") as f:
                texte = f.read(DICTEE_MAX + 1)
    except (OSError, UnicodeError):
        return None
    return texte if texte.strip() and len(texte) <= DICTEE_MAX else None


def _etapes(mocks: bool) -> Etapes | str:
    """Étapes du parcours ; message d'erreur (sans dictée) si la configuration manque."""
    if mocks:
        return etapes_factices()
    try:
        from orchestrator.etapes import etapes_reelles  # extra `media`, Python 3.12

        return etapes_reelles()
    except ErreurConfiguration as exc:
        return f"configuration absente : {exc}"
    except ImportError:
        return "étapes réelles indisponibles : installer l'extra media (Python 3.12)"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dictee = _lire_dictee(args.dictee_file)
    if dictee is None:
        print(
            f"dictée refusée : lisible, non vide, {DICTEE_MAX} caractères au plus", file=sys.stderr
        )
        return EX_USAGE
    etapes = _etapes(args.mocks)
    if isinstance(etapes, str):
        print(etapes, file=sys.stderr)
        return EX_CONFIG
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    out: Path = args.out
    run = lancer(dictee, etapes, workdir=str(out), mode="mocks" if args.mocks else "reel")
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    with open(out / "durees.jsonl", "a", encoding="utf-8") as journal:
        journal.write(json.dumps(ligne_durees(run)) + "\n")
    print(f"run={run['run_id']} etat={run['etat']}")
    return _CODES[run["etat"]]


if __name__ == "__main__":
    sys.exit(main())
