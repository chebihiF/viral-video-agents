"""Design system (#92) : contrastes WCAG AA et rétrocompatibilité de tokens.css."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "templates"
TOKENS = (STATIC / "tokens.css").read_text(encoding="utf-8")

# Noms de jetons v1 : le ticket #92 ne doit en retirer aucun.
LEGACY_TOKENS = [
    "--color-bg", "--color-surface", "--color-surface-2", "--color-text",
    "--color-text-muted", "--color-border", "--color-border-subtle", "--color-accent",
    "--color-on-accent", "--color-focus", "--color-ok", "--color-ok-bg", "--color-warn",
    "--color-warn-bg", "--color-danger", "--color-danger-bg", "--color-info",
    "--color-info-bg", "--font-sans", "--font-mono", "--font-size-title",
    "--font-size-heading", "--font-size-body", "--font-size-small", "--font-size-label",
    "--font-size-data", "--space-1", "--space-2", "--space-3", "--space-4", "--space-5",
    "--space-6", "--space-7", "--radius-sm", "--radius-md", "--radius-lg",
    "--radius-pill", "--focus-ring",
]  # fmt: skip


def _block(selector: str) -> dict[str, str]:
    """Déclarations `--nom: valeur` du bloc de premier niveau commençant par `selector`."""
    match = re.search(rf"^{re.escape(selector)}\s*\{{(.*?)\n\}}", TOKENS, re.S | re.M)
    assert match, f"bloc {selector!r} introuvable"
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", match.group(1)))


LIGHT = _block(':root,\n[data-theme="light"]')
DARK = _block('[data-theme="dark"]')


def _luminance(hex_color: str) -> float:
    h = hex_color.strip().lstrip("#")
    channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg: str, bg: str) -> float:
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_reference_values() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0)


TEXT_PAIRS = [
    ("--color-accent", "--color-accent-bg"),
    ("--color-text", "--color-bg"),
    ("--color-text", "--color-surface"),
    ("--color-text-muted", "--color-bg"),
    ("--color-text-muted", "--color-surface"),
    ("--color-on-accent", "--color-accent"),
    ("--color-ok", "--color-ok-bg"),
    ("--color-warn", "--color-warn-bg"),
    ("--color-danger", "--color-danger-bg"),
    ("--color-info", "--color-info-bg"),
]


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["clair", "sombre"])
@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_contrast_wcag_aa(theme: dict[str, str], fg: str, bg: str) -> None:
    ratio = contrast(theme[fg], theme[bg])
    print(f"{fg} / {bg} = {ratio:.2f}")  # noqa: T201 - visible avec pytest -s
    assert ratio >= 4.5, f"{fg} sur {bg} : {ratio:.2f}:1 < 4,5:1"


@pytest.mark.parametrize("name", LEGACY_TOKENS)
def test_legacy_token_names_still_defined(name: str) -> None:
    assert re.search(rf"^\s*{re.escape(name)}:", TOKENS, re.M), f"{name} supprimé de tokens.css"


def test_templates_link_components_css_not_nav_css() -> None:
    assert not (STATIC / "nav.css").exists()
    assert (STATIC / "components.css").exists()
    for page in TEMPLATES.glob("*.html"):
        assert "nav.css" not in page.read_text(encoding="utf-8"), page.name
