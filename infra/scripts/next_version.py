"""Calcule la prochaine version SemVer à partir des Conventional Commits.

Usage : python infra/scripts/next_version.py  -> affiche « X.Y.Z » (rien si aucune release).
Règles : `feat` -> mineure, `fix`/`perf`/`refactor` -> correctif, `!` ou BREAKING CHANGE -> majeure.
Avant 1.0.0, une rupture fait monter la mineure.
"""

from __future__ import annotations

import re
import subprocess
import sys

CC = re.compile(r"^(?P<type>[a-z]+)(\([^)]*\))?(?P<bang>!)?: ")
PATCH_TYPES = {"fix", "perf", "refactor"}


def bump(current: str, messages: list[str]) -> str | None:
    major, minor, patch = (int(p) for p in current.split("."))
    level = 0  # 0 rien, 1 patch, 2 minor, 3 major
    for msg in messages:
        m = CC.match(msg)
        if not m:
            continue
        if m["bang"] or "BREAKING CHANGE" in msg:
            level = max(level, 3)
        elif m["type"] == "feat":
            level = max(level, 2)
        elif m["type"] in PATCH_TYPES:
            level = max(level, 1)
    if level == 3 and major == 0:
        level = 2
    if level == 3:
        return f"{major + 1}.0.0"
    if level == 2:
        return f"{major}.{minor + 1}.0"
    if level == 1:
        return f"{major}.{minor}.{patch + 1}"
    return None


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False  # noqa: S607
    ).stdout.strip()


def main() -> int:
    last = _git("describe", "--tags", "--abbrev=0", "--match", "v*")
    rng = f"{last}..HEAD" if last else "HEAD"
    log = _git("log", rng, "--format=%B%x00")
    messages = [m.strip() for m in log.split("\x00") if m.strip()]
    nxt = bump(last.removeprefix("v") if last else "0.0.0", messages)
    if nxt:
        print(nxt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
