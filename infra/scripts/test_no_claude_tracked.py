"""Garde-fou : aucun chemin .claude/ ni CLAUDE.md ne doit être suivi par git."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def forbidden(paths: list[str]) -> list[str]:
    return [p for p in paths if ".claude" in Path(p).parts or Path(p).name == "CLAUDE.md"]


def test_forbidden_detecte() -> None:
    assert forbidden([".claude/a", "CLAUDE.md", "x/CLAUDE.md", "x/.claude/b", "docs/a.md"]) == [
        ".claude/a",
        "CLAUDE.md",
        "x/CLAUDE.md",
        "x/.claude/b",
    ]


def test_aucun_fichier_claude_suivi() -> None:
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), "ls-files"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert forbidden(out) == []
