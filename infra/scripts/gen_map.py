"""Génère docs/MAP.md : une ligne par fichier suivi par git (index uniquement) (chemin — résumé).

Le résumé est la première ligne de docstring / commentaire / titre trouvée.
Usage : python3 infra/scripts/gen_map.py [--check]
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "MAP.md"
SKIP = {"uv.lock", "docs/MAP.md"}
DOC = re.compile(r'^\s*(?:"""|#+|//|<!--|title:|name:|description:)\s*(.+?)\s*(?:"""|-->)?$')


def summary(path: Path) -> str:
    if path.suffix == ".json":
        return "données JSON"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[:15]
    except (UnicodeDecodeError, OSError):
        return ""
    for line in lines:
        if line.startswith("#!") or "syntax=" in line or "noqa" in line:
            continue
        m = DOC.match(line)
        if m and len(m[1]) > 3:
            return m[1][:90]
    return ""


def render() -> str:
    files = subprocess.run(
        ["git", "ls-files", "--cached"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    rows = []
    for f in sorted(set(files)):
        if f in SKIP or not (ROOT / f).is_file():
            continue
        s = summary(ROOT / f)
        rows.append(f"- `{f}` — {s}" if s else f"- `{f}`")
    head = "# MAP — un fichier par ligne (généré par `make map`, ne pas éditer)\n\n"
    return head + "\n".join(rows) + "\n"


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != content:
            print("docs/MAP.md est périmé : lancez `make map`")
            return 1
        return 0
    OUT.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
