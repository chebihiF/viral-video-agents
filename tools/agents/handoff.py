#!/usr/bin/env python3
"""Passation automatique de session au seuil de contexte (#83). Voir docs/agents.md.

`check` (hook UserPromptSubmit) demande la note au seuil ; `post` la valide et la poste dans #21 ;
`latest` relit la dernière note ; `relay` poste puis démarre une session neuve avec cette seule
note. Aucun contenu de prompt n'est lu ni écrit. Stdlib ; Python 3.9.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

THRESHOLD = 150_000
ISSUE = "21"
DEFAULT_REPO = "chebihiF/viral-video-agents"
DEFAULT_AUTHORS = "chebihiF,infinitySoftMA"
MARKER = "<!-- passation -->"
MAX_NOTE_LINES = 10
MAX_NOTE_CHARS = 2_000
SECTIONS = {
    "fait": r"fait",
    "reste": r"reste",
    "fichiers clés": r"fichiers\s+cl[ée]s",
    "pièges": r"pi[èe]ges?",
}
SECRETS = re.compile(
    r"(?:gh[pousr]_|github_pat_|xox[abp]-|-----BEGIN)"
    r"|\bsk-[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b"
    r"|(?:token|secret|passw(?:or)?d|api[_-]?key|cl[ée]|bearer)\s*[:=]\s*\S{8,}"
    r"|\b[A-Za-z0-9]{32,}\b",
    re.IGNORECASE,
)
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TAIL_BYTES = 1_000_000
GH_TIMEOUT_S = 30
STARTUP_WAIT_S = 5.0
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

# Alias évalués à l'exécution : `X | None` plante en 3.9, d'où Optional.
Gh = Callable[[Sequence[str], Optional[str]], str]  # noqa: UP007, UP045
Launch = Callable[[Sequence[str], str], None]


class HandoffError(Exception):
    """Échec attendu (note invalide, gh ou session KO) : une ligne pour le parent."""


# --- check : seuil de contexte ---------------------------------------------------------


def context_tokens(transcript: Path) -> int | None:
    """Contexte du dernier tour du fil principal : input + cache lu + cache créé."""
    try:
        with transcript.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TAIL_BYTES))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        total = _usage_total(line)
        if total:
            return total
    return None


def _usage_total(line: str) -> int:
    try:
        entry = json.loads(line)
    except ValueError:
        return 0
    if not isinstance(entry, dict) or entry.get("isSidechain") is True:
        return 0
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return 0
    return sum(v for k in USAGE_KEYS if type(v := usage.get(k, 0)) is int)


def _claim(state_dir: Path, session: str) -> bool:
    """Marqueur local, créé de façon atomique : vrai la première fois seulement."""
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(state_dir / f"{session}.done", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _state_dir() -> Path:
    return Path(os.environ.get("VVA_HANDOFF_DIR") or Path.home() / ".vva" / "handoff")


def _spaced(n: int) -> str:
    return f"{n:_}".replace("_", " ")


def request_text(tokens: int, threshold: int) -> str:
    return (
        f"Passation de session : le contexte atteint {_spaced(tokens)} tokens "
        f"(seuil {_spaced(threshold)}). "
        "Avant toute autre action : écris la note de passation dans un fichier temporaire "
        f"(≤ {MAX_NOTE_LINES} lignes : Fait / Reste / Fichiers clés / Pièges ; aucun secret ni "
        "donnée d'enfant), puis lance `tools/agents/bin/handoff.sh relay --note-file <fichier>` "
        f"(poste la note dans #{ISSUE} et démarre une session neuve qui la reprend)."
    )


def check(hook_text: str, threshold: int = THRESHOLD) -> str:
    """Hook UserPromptSubmit : JSON de hook au seuil (une fois par session), sinon rien."""
    try:
        hook = json.loads(hook_text)
        session, path = hook["session_id"], hook["transcript_path"]
        if not (isinstance(session, str) and SESSION_ID.match(session) and isinstance(path, str)):
            return ""
        tokens = context_tokens(Path(path))
        if tokens is None or tokens < threshold or not _claim(_state_dir(), session):
            return ""
        context = request_text(tokens, threshold)
        output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}
        output["hookSpecificOutput"]["additionalContext"] = context
        return json.dumps(output, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - un hook ne bloque jamais la session
        return ""


# --- note de passation ------------------------------------------------------------------


def validate_note(text: str) -> str:
    """La note nettoyée, ou HandoffError : ≤ 10 lignes, 4 rubriques, aucun motif de secret."""
    lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise HandoffError("passation : note vide")
    if len(lines) > MAX_NOTE_LINES or len(text) > MAX_NOTE_CHARS:
        raise HandoffError(f"passation : note trop longue (≤ {MAX_NOTE_LINES} lignes)")
    body = "\n".join(lines)
    for name, pattern in SECTIONS.items():
        if not re.search(rf"^\W*{pattern}\b", body, re.IGNORECASE | re.MULTILINE):
            raise HandoffError(f"passation : rubrique « {name} » manquante")
    if SECRETS.search(body):
        raise HandoffError("passation : la note ressemble à un secret, refusée")
    return body


def _read_note(path: Path) -> str:
    try:
        return validate_note(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise HandoffError(f"passation : note illisible ({path.name})") from exc


def _repo() -> list[str]:
    return ["-R", os.environ.get("HANDOFF_REPO") or DEFAULT_REPO]


def post(note: str, gh: Gh) -> None:
    gh(["issue", "comment", ISSUE, *_repo(), "--body-file", "-"], f"{MARKER}\n{note}\n")


def latest(gh: Gh, env: Mapping[str, str] = os.environ) -> str:
    """Dernière note marquée de #21, d'un auteur autorisé ; jamais l'historique."""
    allowed = set((env.get("HANDOFF_AUTHORS") or DEFAULT_AUTHORS).split(","))
    raw = gh(["issue", "view", ISSUE, *_repo(), "--json", "comments"], None)
    try:
        comments: list[Any] = json.loads(raw)["comments"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HandoffError("passation : réponse gh illisible") from exc
    for comment in reversed(comments):
        body = comment.get("body") if isinstance(comment, dict) else None
        author = (comment.get("author") or {}).get("login") if isinstance(body, str) else None
        if author in allowed and body is not None and body.lstrip().startswith(MARKER):
            return validate_note(body.lstrip()[len(MARKER) :])
    raise HandoffError(f"passation : aucune note marquée dans #{ISSUE}")


# --- session neuve ----------------------------------------------------------------------


def session_command(env: Mapping[str, str]) -> list[str]:
    return shlex.split(env.get("HANDOFF_CMD") or "claude -p")


def reprise_prompt(note: str) -> str:
    return (
        f"Tu reprends le travail d'une session précédente. Ton seul contexte est la dernière "
        f"note de passation de #{ISSUE} ci-dessous ; ne relis pas l'historique de #{ISSUE}.\n\n"
        f"{note}\n"
    )


def _session_log() -> int:
    """Log local de la session relayée (sa sortie seule, jamais le prompt) ; sinon poubelle."""
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        return os.open(directory / "relay.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        return subprocess.DEVNULL


def launch_session(command: Sequence[str], prompt: str) -> None:
    """Démarre la commande détachée, prompt sur stdin ; échoue si elle s'arrête en erreur vite."""
    log = _session_log()
    try:
        proc = subprocess.Popen(  # noqa: S603 - commande fixée par le parent (HANDOFF_CMD)
            list(command),
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        code = proc.wait(timeout=STARTUP_WAIT_S)
    except subprocess.TimeoutExpired:
        return  # toujours en cours : la session a démarré
    except (OSError, ValueError) as exc:
        raise HandoffError(f"démarrage impossible ({type(exc).__name__})") from exc
    finally:
        if log >= 0:  # subprocess.DEVNULL est négatif
            os.close(log)
    if code != 0:
        raise HandoffError(f"démarrage refusé (code {code})")


# --- gh ---------------------------------------------------------------------------------


def gh_cli(args: Sequence[str], stdin: str | None = None) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - gh du poste, arguments fixes
            ["gh", *args],  # noqa: S607
            input=stdin,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HandoffError(f"passation : gh a échoué ({type(exc).__name__})") from exc
    return done.stdout


@dataclass
class Deps:
    gh: Gh = field(default=gh_cli)
    launch: Launch = field(default=launch_session)


# --- ligne de commande ------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    chk = sub.add_parser("check", help="hook UserPromptSubmit : JSON du hook sur stdin")
    chk.add_argument("--threshold", type=int, default=THRESHOLD)
    for name, text in (("post", "valider et poster la note"), ("relay", "poster puis relayer")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--note-file", type=Path, required=True)
    sub.add_parser("latest", help="dernière note marquée de #21")
    return parser


def _relay(note: str, deps: Deps) -> None:
    post(note, deps.gh)
    try:
        prompt = reprise_prompt(latest(deps.gh))
        deps.launch(session_command(os.environ), prompt)
    except HandoffError as exc:
        why = str(exc).removeprefix("passation : ")
        print(
            f"passation : note postée dans #{ISSUE}, session non démarrée ({why})", file=sys.stderr
        )


def _execute(args: argparse.Namespace, stdin: str, deps: Deps) -> None:
    if args.command == "check":
        output = check(stdin, args.threshold)
        if output:
            print(output)
    elif args.command == "latest":
        print(latest(deps.gh))
    elif args.command == "post":
        post(_read_note(args.note_file), deps.gh)
    else:
        _relay(_read_note(args.note_file), deps)


def main(
    argv: Sequence[str] | None = None, *, stdin: str | None = None, deps: Deps | None = None
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "check" and stdin is None:
        stdin = sys.stdin.read()
    try:
        _execute(args, stdin or "", deps or Deps())
    except HandoffError as exc:
        print(str(exc).splitlines()[0], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
