"""CLI de l'orchestrateur (#138) : `python -m orchestrator run --mocks --dictee-file F --out DIR`.

Codes de sortie : 0 ok, 1 erreur, 2 bloque, 64 usage/dictée invalide, 78 configuration absente
(sans `--mocks` : OLLAMA_URL, PIPER_BIN, PIPER_MODEL, COMFY_URL en boucle locale ; échec fermé
avant tout appel réseau).
Sous-commande `purge` (#152) : codes 0 ok, 1 suppression échouée, 2 usage.
Sous-commande `galerie` (#151) : page HTML statique des runs ; codes 0 ok, 1 écriture, 2 usage.
Image et clip réels via l'API ComfyUI (#245) : clips téléchargés sous `<out>/<run_id>/`.
La dictée n'est ni écrite, ni journalisée, ni affichée.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from orchestrator.galerie import generer
from orchestrator.mocks import etapes_factices
from orchestrator.pipeline import Etapes, lancer, ligne_durees
from orchestrator.purge import supprimer, trouver_anciens
from orchestrator.steps_llm import ErreurConfiguration

DICTEE_MAX = 2000
EX_USAGE = 64
EX_PURGE_USAGE = 2
EX_CONFIG = 78
_CODES = {"ok": 0, "erreur": 1, "bloque": 2}


class _Parser(argparse.ArgumentParser):
    code_usage = EX_USAGE

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(self.code_usage, f"orchestrator: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orchestrator", description="Orchestrateur du pipeline vidéo.")
    sub = parser.add_subparsers(dest="commande", required=True, parser_class=_Parser)
    run = sub.add_parser("run", help="lance un parcours depuis une dictée texte")
    run.add_argument("--dictee-file", required=True, help="fichier de dictée UTF-8, « - » = stdin")
    run.add_argument(
        "--out", required=True, type=Path, help="dossier de sortie (run.json et durees.jsonl)"
    )
    run.add_argument("--mocks", action="store_true", help="étapes factices (sans GPU)")
    purge = sub.add_parser("purge", help="supprime les runs plus anciens que N jours")
    purge.code_usage = EX_PURGE_USAGE
    purge.add_argument("--runs", required=True, type=Path, help="dossier contenant les runs")
    purge.add_argument("--older-than-days", required=True, type=int, metavar="N", help="N >= 1")
    purge.add_argument("--apply", action="store_true", help="supprime (sinon : liste seulement)")
    purge.add_argument("--dry-run", action="store_true", help="liste sans supprimer (défaut)")
    galerie = sub.add_parser("galerie", help="génère une page HTML locale des vidéos produites")
    galerie.code_usage = EX_PURGE_USAGE
    galerie.add_argument("--runs", required=True, type=Path, help="dossier contenant les runs")
    galerie.add_argument("--out", required=True, type=Path, help="fichier HTML à écrire")
    return parser


def _galerie(args: argparse.Namespace) -> int:
    """Génère la galerie ; n'imprime que le nombre de runs."""
    racine: Path = args.runs
    if racine.is_symlink() or not racine.is_dir():
        print("dossier des runs absent ou invalide", file=sys.stderr)
        return EX_PURGE_USAGE
    try:
        total = generer(racine, args.out)
    except OSError:
        print("galerie non écrite : dossier des runs ou sortie inaccessible", file=sys.stderr)
        return 1
    print(f"galerie runs={total}")
    return 0


def _purger(args: argparse.Namespace) -> int:
    """Liste (défaut) ou supprime (`--apply`) ; n'imprime que run_id et âge."""
    racine: Path = args.runs
    if args.older_than_days < 1:
        print("--older-than-days doit être >= 1", file=sys.stderr)
        return EX_PURGE_USAGE
    if args.apply and args.dry_run:
        print("--apply et --dry-run s'excluent", file=sys.stderr)
        return EX_PURGE_USAGE
    if racine.is_symlink() or not racine.is_dir():
        print("dossier des runs absent ou invalide", file=sys.stderr)
        return EX_PURGE_USAGE
    if args.apply and not shutil.rmtree.avoids_symlink_attacks:
        print("suppression refusée : plateforme sans rmtree résistant aux liens", file=sys.stderr)
        return EX_PURGE_USAGE
    try:
        anciens = trouver_anciens(racine, args.older_than_days)
    except OSError:
        print("dossier des runs illisible", file=sys.stderr)
        return EX_PURGE_USAGE
    code = 0
    for run in anciens:
        etat = "a_supprimer"
        if args.apply:
            try:
                supprimer(run)
                etat = "supprime"
            except OSError as exc:
                etat = f"echec({type(exc).__name__})"
                code = 1
        print(f"{run.run_id} age_jours={run.age_jours} {etat}")
    return code


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


def _etapes(mocks: bool, out: Path) -> Etapes | str:
    """Étapes du parcours ; message d'erreur (sans dictée) si la configuration manque."""
    if mocks:
        return etapes_factices()
    try:
        from orchestrator.etapes import etapes_reelles  # extra `media`, Python 3.12

        return etapes_reelles(out=out)
    except ErreurConfiguration as exc:
        return f"configuration absente : {exc}"
    except ImportError:
        return "étapes réelles indisponibles : installer l'extra media (Python 3.12)"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.commande == "purge":
        return _purger(args)
    if args.commande == "galerie":
        return _galerie(args)
    dictee = _lire_dictee(args.dictee_file)
    if dictee is None:
        print(
            f"dictée refusée : lisible, non vide, {DICTEE_MAX} caractères au plus", file=sys.stderr
        )
        return EX_USAGE
    out: Path = args.out
    etapes = _etapes(args.mocks, out)
    if isinstance(etapes, str):
        print(etapes, file=sys.stderr)
        return EX_CONFIG
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run = lancer(dictee, etapes, workdir=str(out), mode="mocks" if args.mocks else "reel")
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    with open(out / "durees.jsonl", "a", encoding="utf-8") as journal:
        journal.write(json.dumps(ligne_durees(run)) + "\n")
    print(f"run={run['run_id']} etat={run['etat']}")
    return _CODES[run["etat"]]


if __name__ == "__main__":
    sys.exit(main())
