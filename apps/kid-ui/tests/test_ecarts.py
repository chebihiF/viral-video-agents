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
    faces = [f for f in re.findall(r"@font-face\s*\{(.*?)\}", CSS, re.S) if "Tajawal" in f]
    assert len(faces) == 3
    for face in faces:
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


# --- #222 : polices latines, liseré de Mimo, écran erreur, chip durée, radios au clavier ---
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
SW = (STATIC / "sw.js").read_text(encoding="utf-8")
LATIN = {"Bricolage Grotesque": (700, 800), "Figtree": (500, 600, 700, 800)}


def faces_latines() -> list[str]:
    return [f for f in re.findall(r"@font-face\s*\{(.*?)\}", CSS, re.S) if "Tajawal" not in f]


def test_font_face_latines_locales_woff2() -> None:
    faces = faces_latines()
    assert len(faces) == sum(len(v) for v in LATIN.values())
    for famille, graisses in LATIN.items():
        for graisse in graisses:
            face = next(
                f
                for f in faces
                if f'font-family: "{famille}"' in f and f"font-weight: {graisse};" in f
            )
            assert "font-display: swap" in face
            assert "unicode-range" in face
            url = re.search(r'url\("([^"]+)"\)', face)
            assert url and not re.match(r"^(https?:)?//", url[1]), "pas de CDN"
            assert (STATIC / url[1]).read_bytes()[:4] == b"wOF2"
            assert f"/{url[1]}" in SW, "précaché par sw.js"


def test_licences_ofl_des_polices_latines() -> None:
    for nom in ("Bricolage-Grotesque", "Figtree"):
        texte = (FONTS / f"OFL-{nom}.txt").read_text(encoding="utf-8")
        assert "SIL Open Font License" in texte


def test_mimo_a_un_lisere_clair_la_nuit_seulement() -> None:
    assert re.search(r"\.mimo\s*\{[^}]*filter:\s*var\(--mimo-halo,\s*none\)", CSS)
    nuit = re.search(r":root, \[data-theme=\"nuit\"\] \{(.*?)\n\}", CSS, re.S)
    assert nuit and re.search(r"--mimo-halo:\s*drop-shadow\(", nuit[1])
    for theme in ("soleil", "menthe"):
        bloc = re.search(rf"\[data-theme=\"{theme}\"\] \{{(.*?)\n\}}", CSS, re.S)
        assert bloc and re.search(r"--mimo-halo:\s*none", bloc[1]), theme
    clair = re.search(r"prefers-color-scheme: light\) \{(.*?)\n\}\n", CSS, re.S)
    assert clair and "--mimo-halo: none" in clair[1]


def test_ecran_erreur_masque_au_depart_et_titre_mimo() -> None:
    assert re.search(r'<section[^>]*id="ecran-erreur"[^>]*\bhidden\b', INDEX)
    assert not re.search(r'<section[^>]*id="ecran-accueil"[^>]*\bhidden\b', INDEX)
    assert "<title>Mimo</title>" in INDEX
    for js in STATIC.glob("*.js"):
        assert "document.title" not in js.read_text(encoding="utf-8"), js.name


def test_plus_d_ancres_idee_provisoires() -> None:
    assert "#idee-" not in INDEX
    assert len(re.findall(r'<button type="button" class="kid-btn" data-idee="', INDEX)) == 3


def test_chip_duree_et_duree_du_poster_stylees() -> None:
    assert re.search(r"\.kid-chip\.dur\s*\{[^}]*font-variant-numeric:\s*tabular-nums", CSS)
    poster = regle(".poster .dur")
    assert "position: absolute" in poster and "background: var(--surface)" in poster


def test_radios_fleches_clavier() -> None:
    assert '<script src="/accueil.js"></script>' in INDEX
    assert "/accueil.js" in SW
    js = (STATIC / "accueil.js").read_text(encoding="utf-8")
    for touche in ("ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"):
        assert f'"{touche}"' in js, touche
    assert "radiogroup" in js and "tabindex" in js and ".focus()" in js and ".click()" in js
    assert 'direction === "rtl"' in js  # en RTL la flèche droite recule
    assert "fetch(" not in js and "XMLHttpRequest" not in js
