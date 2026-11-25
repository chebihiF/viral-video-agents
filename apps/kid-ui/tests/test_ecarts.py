"""Écarts de la spec #36 (kid-ui.md) : police arabe, reduced-motion, RTL, cibles 48 px."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import server

STATIC = Path(server.STATIC_DIR)
CSS = (STATIC / "tokens-enfant.css").read_text(encoding="utf-8")
FONTS = STATIC / "fonts"


def regle(selecteur: str) -> str:
    """Corps de la règle dont le sélecteur est exactement `selecteur`."""
    m = re.search(rf"^{re.escape(selecteur)}\s*\{{(.*?)\}}", CSS, re.S | re.M)
    assert m, selecteur
    return m.group(1)


def test_font_face_arabe_local() -> None:
    faces = re.findall(r"@font-face\s*\{(.*?)\}", CSS, re.S)
    assert len(faces) >= 3
    for face in faces:
        assert 'font-family: "Tajawal"' in face
        assert "font-display: swap" in face
        assert "unicode-range" in face  # l'arabe seulement : le latin garde sa pile
        url = re.search(r'url\("([^"]+)"\)', face)
        assert url and not re.match(r"^(https?:)?//", url[1]), "pas de CDN"
        assert (STATIC / url[1]).is_file()
        assert (STATIC / url[1]).read_bytes()[:4] == b"wOF2"


def test_police_arabe_dans_les_deux_piles_et_licence() -> None:
    for var in ("--font-display", "--font-body"):
        pile = re.search(rf"{var}:([^;]+);", CSS)
        assert pile and '"Tajawal"' in pile[1], var
    licence = (FONTS / "OFL-Tajawal.txt").read_text(encoding="utf-8")
    assert "SIL Open Font License" in licence


def test_aucune_url_externe_dans_les_css() -> None:
    for nom in ("tokens-enfant.css", "shell.css"):
        texte = (STATIC / nom).read_text(encoding="utf-8")
        assert not re.search(r"url\(\s*[\"']?(https?:)?//", texte), nom


def test_confettis_masques_sous_reduced_motion() -> None:
    m = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", CSS, re.S)
    assert m
    assert re.search(r"\.confetti\s*\{\s*display:\s*none;\s*\}", m[1])


@pytest.mark.parametrize(
    "selecteur",
    [
        ".poster .play",
        ".kid-composer textarea",
        ".kid-theme",
        ".variants button",
        ".starters .kid-btn",
    ],
)
def test_rtl_pas_de_propriete_physique(selecteur: str) -> None:
    css = next(
        (m for m in re.findall(r"^([^{}\n]*)\{(.*?)\}", CSS, re.S | re.M) if selecteur in m[0]),
        None,
    )
    assert css, selecteur
    corps = css[1]
    quatre = r"\b(margin|padding):\s*[^;]*\s[^;]*\s[^;]*\s[^;]*;"
    assert not re.search(quatre, corps), "forme à 4 valeurs"
    physique = r"\b(margin|padding)-(left|right)\b|(?<![-\w])(left|right):"
    assert not re.search(physique, corps)


def test_rtl_bouton_lecture_centre_en_logique() -> None:
    corps = regle(".poster .play")
    assert "margin-inline: -28px 0" in corps and "margin-block: -28px 0" in corps
    assert "inset-inline-start: 50%" in corps


def test_cibles_tactiles_48() -> None:
    assert re.search(r"--touch:\s*48px", CSS)
    icone = regle(".kid-icon-btn")
    assert "width: var(--touch)" in icone and "height: var(--touch)" in icone
    assert "46px" not in icone
    assert "min-height: var(--touch)" in regle(".kid-composer textarea")
