"""Message « j'allume le PC » (#230) quand le job est en réveil (`waking: true`, kid-job v1.2).

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
let etat = {{ state: "queued", step: "storyboard" }};
const routes = (url, init) => {{
  if (url === "/api/jobs" && init.method === "POST")
    return {{ status: 202, corps: {{ job_id: JOB }} }};
  if (url === "/api/jobs/" + JOB) return {{ status: 200, corps: etat }};
}};
async function lancer(ui) {{
  ui.win.MimoStatus.envoyer(IDEE);
  await ui.flush();
  await ui.sonder(); // premier statut : l'état courant du job
}}
const zone = (ui) => ui.q('.waking');
const dit = (ui) => (zone(ui) ? zone(ui).textContent : "");
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


# --- Scénario : job queued avec waking: true ---
@needs_node
def test_queued_en_reveil_affiche_j_allume_le_pc_avec_pictogramme() -> None:
    r = run("""
      const ui = demarrer({ routes });
      etat = { state: "queued", step: "storyboard", waking: true };
      await lancer(ui);
      const svgs = zone(ui).querySelectorAll("svg");
      console.log(JSON.stringify({ texte: dit(ui), svg: svgs.length,
        masque: svgs.every((s) => s.getAttribute("aria-hidden") === "true") }));
    """)
    assert "j'allume le pc" in r["texte"].lower()
    assert r["svg"] == 1
    assert r["masque"]


# --- Scénario : waking absent ou false ---
@needs_node
@pytest.mark.parametrize("extra", ["", ", waking: false"])
def test_sans_waking_pas_de_message_et_le_message_queued_reste(extra: str) -> None:
    r = run(f"""
      const ui = demarrer({{ routes }});
      etat = {{ state: "queued", step: "storyboard"{extra} }};
      await lancer(ui);
      console.log(JSON.stringify({{ texte: dit(ui), say: ui.q(".say").textContent }}));
    """)
    assert r["texte"] == ""
    assert r["say"] == "J'écris l'histoire…"


# --- Scénario : région aria-live polite, sans texte de dictée ---
@needs_node
def test_region_aria_live_polite_sans_texte_de_dictee() -> None:
    r = run("""
      const ui = demarrer({ routes });
      etat = { state: "queued", step: "storyboard", waking: true };
      await lancer(ui);
      const z = zone(ui);
      console.log(JSON.stringify({ live: z.getAttribute("aria-live"), role: z.getAttribute("role"),
        dictee: ui.q("#ecran-statut").textContent.includes(IDEE) }));
    """)
    assert r["live"] == "polite"
    assert r["role"] == "status"
    assert not r["dictee"]


# --- Scénario : waking repasse à absent ---
@needs_node
def test_le_message_disparait_quand_waking_repasse_a_absent() -> None:
    r = run("""
      const ui = demarrer({ routes });
      etat = { state: "queued", step: "storyboard", waking: true };
      await lancer(ui);
      const avant = dit(ui);
      const region = zone(ui);
      etat = { state: "running", step: "storyboard" };
      await ui.sonder();
      console.log(JSON.stringify({ avant, apres: dit(ui), memeRegion: zone(ui) === region }));
    """)
    assert r["avant"] != ""
    assert r["apres"] == ""
    assert r["memeRegion"]  # la région vit toujours : l'annonce suivante reste possible
