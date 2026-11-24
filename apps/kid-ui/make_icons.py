"""Icônes PNG de l'appli (mascotte Mimo), stdlib seule : python3 apps/kid-ui/make_icons.py."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "static" / "icons"
BG = (0x12, 0x0E, 0x2B)  # fond du thème nuit
ORANGE, CREAM, INK, PINK = (
    (0xFF, 0x8A, 0x1F),
    (0xFF, 0xE3, 0xC2),
    (0x1B, 0x13, 0x40),
    (0xFF, 0x5C, 0x8A),
)

Pt = tuple[float, float]
# Mascotte sur la grille 64x64 de la maquette : (type, géométrie, couleur), du fond vers l'avant
SHAPES: list[tuple[str, tuple[float, ...], tuple[int, int, int]]] = [
    ("tri", (10, 28, 12, 6, 28, 16), ORANGE),
    ("tri", (54, 28, 52, 6, 36, 16), ORANGE),
    ("ell", (32, 36, 25, 21), ORANGE),
    ("ell", (32, 43, 10, 7), CREAM),
    ("ell", (22, 34, 3.6, 4.6), INK),
    ("ell", (42, 34, 3.6, 4.6), INK),
    ("tri", (29.5, 40, 34.5, 40, 32, 43), PINK),
]


def _in_tri(x: float, y: float, g: tuple[float, ...]) -> bool:
    def s(ax: float, ay: float, bx: float, by: float) -> float:
        return (x - bx) * (ay - by) - (ax - bx) * (y - by)

    d1, d2, d3 = s(g[0], g[1], g[2], g[3]), s(g[2], g[3], g[4], g[5]), s(g[4], g[5], g[0], g[1])
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def _color_at(u: float, v: float) -> tuple[int, int, int]:
    """Couleur au point (u, v) de la grille 64x64 (dernière forme touchée)."""
    out = BG
    for kind, g, col in SHAPES:
        if kind == "tri":
            hit = _in_tri(u, v, g)
        else:
            hit = ((u - g[0]) / g[2]) ** 2 + ((v - g[1]) / g[3]) ** 2 <= 1
        if hit:
            out = col
    return out


def render(size: int, scale: float, ss: int = 2) -> bytes:
    """Image RGB `size` px, mascotte occupant `scale` du côté, suréchantillonnée ss x ss."""
    k = 64 / (size * scale)  # pixel -> grille mascotte
    off = (size - size * scale) / 2
    rows = []
    for py in range(size):
        row = bytearray([0])  # filtre PNG « None »
        for px in range(size):
            acc = [0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    c = _color_at(
                        (px + (sx + 0.5) / ss - off) * k, (py + (sy + 0.5) / ss - off) * k
                    )
                    acc[0] += c[0]
                    acc[1] += c[1]
                    acc[2] += c[2]
            row += bytes(a // (ss * ss) for a in acc)
        rows.append(bytes(row))
    return b"".join(rows)


def png(size: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8 bits, RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # maskable : mascotte dans la zone sûre (disque de 80 %) ; "any" : plus grande
    for name, size, scale in (
        ("icon-192.png", 192, 0.8),
        ("icon-512.png", 512, 0.8),
        ("maskable-512.png", 512, 0.58),
    ):
        (OUT / name).write_bytes(png(size, render(size, scale)))
        print("écrit", OUT / name)  # noqa: T201


if __name__ == "__main__":
    main()
