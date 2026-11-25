"""Bouton Envoyer du clavier (#221) : le texte tapé part par MimoStatus.envoyer.

Les vrais scripts de static/ tournent sous node dans le mini DOM (tests/minidom.js).
Textes synthétiques.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

MINIDOM = Path(__file__).resolve().parent / "minidom.js"
IDEE = "Un chat astronaute qui joue du piano"  # synthétique
JOB = "0123abcd-0123-4abc-8abc-0123456789ab"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node absent")

PRELUDE = f"""
const {{ demarrer }} = require({json.dumps(str(MINIDOM))});
const IDEE = {json.dumps(IDEE)};
const JOB = {json.dumps(JOB)};
const routes = (url, init) => {{
  if (url === "/api/jobs" && init.method === "POST")
    return {{ status: 202, corps: {{ job_id: JOB }} }};
}};
// Comme un navigateur : saisie -> événement input ; flèche -> événement submit du formulaire.
function taper(ui, t) {{
  const zone = ui.q("#idee");
  zone.value = t;
  zone.dispatchEvent(new ui.win.Evt("input"));
}}
function fleche(ui) {{
  const b = ui.q('.kid-composer button[type=submit]');
  if (!b.disabled) ui.q(".kid-composer").dispatchEvent(new ui.win.Evt("submit"));
}}
const envois = (ui) => ui.appels.filter((a) => a.url === "/api/jobs").map((a) => a.json.text);
"""


def run(script: str) -> Any:
    code = (
        PRELUDE
        + "(async () => {"
        + script
        + "})().catch((e) => { console.error(e); process.exit(1); });"
    )
    out = subprocess.run(  # noqa: S603
        ["node", "-e", code],  # noqa: S607
        capture_output=True, text=True, check=False, timeout=20,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_envoi_du_texte_saisi() -> None:
    r = run("""
      const ui = demarrer({ routes });
      taper(ui, "  " + IDEE + "  ");
      fleche(ui);
      await ui.flush();
      console.log(JSON.stringify({ envois: envois(ui), visibles: ui.visibles(),
        vide: ui.q("#idee").value }));
    """)
    assert r["envois"] == [IDEE]
    assert r["visibles"] == ["ecran-statut"]
    assert r["vide"] == ""  # la zone est vidée : « Autre idée » repart d'une page blanche


@needs_node
def test_texte_de_600_caracteres_tronque_a_500() -> None:
    r = run("""
      const ui = demarrer({ routes });
      taper(ui, "a".repeat(600));
      fleche(ui);
      await ui.flush();
      console.log(JSON.stringify(envois(ui).map((t) => t.length)));
    """)
    assert r == [500]


@needs_node
def test_zone_vide_ou_d_espaces_fleche_desactivee_et_aucun_envoi() -> None:
    r = run("""
      const ui = demarrer({ routes });
      const b = ui.q('.kid-composer button[type=submit]');
      const depart = b.disabled;
      taper(ui, "   \\n ");
      const espaces = b.disabled;
      ui.q(".kid-composer").dispatchEvent(new ui.win.Evt("submit")); // même forcé, rien ne part
      await ui.flush();
      taper(ui, IDEE);
      const saisi = b.disabled;
      taper(ui, "");
      console.log(JSON.stringify({ depart, espaces, saisi, apres: b.disabled,
        appels: ui.appels.length }));
    """)
    assert r == {"depart": True, "espaces": True, "saisi": False, "apres": True, "appels": 0}


def test_shell_appelle_mimostatus_envoyer_sans_innerhtml() -> None:
    js = (Path(__file__).resolve().parents[1] / "static" / "shell.js").read_text("utf-8")
    assert "MimoStatus.envoyer(" in js
    assert "innerHTML" not in js
