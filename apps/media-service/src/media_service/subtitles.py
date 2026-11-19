"""Sous-titres ASS mot par mot : calcul pur à partir du texte et de la durée de chaque plan.

Un événement par mot, durée proportionnelle à la longueur du mot (1 centième minimum),
décalage cumulé entre plans. Les bornes sont arrondies au centième (précision ASS) sur le
temps cumulé, donc les événements sont contigus : aucun trou, aucun chevauchement.
Stdlib uniquement.
"""

import math
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

WIDTH, HEIGHT = 1080, 1920
FONT = "DejaVu Sans"  # couvre l'arabe et le latin ; libass se rabat sur fontconfig sinon
FONT_SIZE = 84
_MARKUP = str.maketrans({"{": "(", "}": ")", "\\": "/"})  # libass lit {\an8} et \N

_STYLE_FIELDS = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
    "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
_STYLE = (
    f"Word,{FONT},{FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
    "1,0,0,0,100,100,0,0,1,5,1,2,60,60,260,1"
)
_HEADER = "\n".join(
    [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {WIDTH}",
        f"PlayResY: {HEIGHT}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        f"Format: {_STYLE_FIELDS}",
        f"Style: {_STYLE}",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "",
    ]
)


class SubtitleError(ValueError):
    """Entrée de sous-titrage invalide."""


@dataclass(frozen=True)
class ShotText:
    texte: str
    duree_s: float


def _stamp(centis: int) -> str:
    h, rest = divmod(centis, 360_000)
    m, rest = divmod(rest, 6000)
    s, cs = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _shot_events(text: str, start_s: float, duration_s: float, n: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    first = round(start_s * 100)
    spare = round((start_s + duration_s) * 100) - first - len(words)  # 1 centième par mot
    if spare < 0:
        raise SubtitleError(f"plan {n} : {len(words)} mots ne tiennent pas en {duration_s} s")
    total = sum(len(w) for w in words)
    lines: list[str] = []
    done = 0
    previous = first
    for count, word in enumerate(words, start=1):
        done += len(word)
        end = first + count + round(spare * done / total)
        clean = word.translate(_MARKUP)
        lines.append(f"Dialogue: 0,{_stamp(previous)},{_stamp(end)},Word,,0,0,0,,{clean}")
        previous = end
    return lines


def build_ass(shots: Sequence[ShotText]) -> str:
    """Contenu ASS (v4.00+, 1080x1920) : un événement par mot, plans mis bout à bout."""
    lines: list[str] = []
    offset = 0.0
    for n, shot in enumerate(shots, start=1):
        if not math.isfinite(shot.duree_s) or shot.duree_s <= 0:
            raise SubtitleError(f"plan {n} : duree_s > 0 attendue, reçu {shot.duree_s!r}")
        lines += _shot_events(shot.texte, offset, shot.duree_s, n)
        offset += shot.duree_s
    return _HEADER + "\n".join(lines) + "\n"


def write_ass(shots: Sequence[ShotText], path: Path) -> Path:
    """Écrit le fichier .ass en UTF-8, de façon atomique (temporaire puis rename)."""
    content = build_ass(shots)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sub-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
