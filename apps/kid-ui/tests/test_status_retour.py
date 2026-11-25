"""Suivi rafraîchi au retour de l'enfant (#233) : `visibilitychange` (visible) et `online`.

Les vrais scripts de static/ tournent sous node dans le mini DOM (tests/minidom.js).
Lecture seule de GET /api/jobs/<id>. Textes synthétiques.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

MINIDOM = Path(__file__).resolve().parent / "minidom.js"
DOC = Path(__file__).resolve().parents[3] / "docs" / "design" / "kid-ui.md"
IDEE = "Un chat astronaute qui joue du piano"  # synthétique
JOB = "0123abcd-0123-4abc-8abc-0123456789ab"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node absent")

PRELUDE = f"""
const {{ demarrer }} = require({json.dumps(str(MINIDOM))});
const IDEE = {json.dumps(IDEE)};
const JOB = {json.dumps(JOB)};
let etat = {{ state: "running", step: "images", progress: {{ pct: 40 }} }};
let coupe = false;
const routes = (url, init) => {{
  if (url === "/api/jobs" && init.method === "POST")
    return {{ status: 202, corps: {{ job_id: JOB }} }};
  if (url === "/api/jobs/" + JOB)
    return coupe ? {{ reseau: false }} : {{ status: 200, corps: etat }};
}};
const gets = (ui) => ui.appels.filter((a) => a.method === "GET").length;
const vivants = (ui) => ui.minuteurs.filter((m) => m.ms === 2000).length;
const valeur = (ui) => ui.q(".kid-seg").getAttribute("aria-valuenow");
async function lancer(ui) {{
  ui.win.MimoStatus.envoyer(IDEE);
  await ui.flush();
  await ui.sonder(); // premier statut : job en cours, polling armé
}}
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


# --- Scénario : retour au premier plan ---
@needs_node
def test_retour_au_premier_plan_un_seul_appel_et_etat_affiche() -> None:
    r = run("""
      const ui = demarrer({ routes });
      await lancer(ui);
      const avant = gets(ui);
      const v0 = valeur(ui);
      ui.visibilite("hidden");
      await ui.flush();
      const enFond = gets(ui) - avant;
      etat = { state: "running", step: "images", progress: { pct: 70 } };
      ui.visibilite("visible");
      await ui.flush();
      console.log(JSON.stringify({ enFond, appels: gets(ui) - avant, v0, v1: valeur(ui),
        url: ui.appels[ui.appels.length - 1].url, timers: vivants(ui) }));
    """)
    assert r["enFond"] == 0  # passer en arrière-plan n'interroge pas
    assert r["appels"] == 1
    assert (r["v0"], r["v1"]) == ("40", "70")
    assert r["url"].endswith("/api/jobs/0123abcd-0123-4abc-8abc-0123456789ab")
    assert r["timers"] == 1  # le minuteur est réarmé


# --- Scénario : job terminé pendant l'absence ---
@needs_node
def test_job_pret_pendant_l_absence_affiche_video_prete_et_arrete_le_polling() -> None:
    r = run("""
      const ui = demarrer({ routes });
      await lancer(ui);
      etat = { state: "done" };
      ui.visibilite("visible");
      await ui.flush();
      const apres = gets(ui);
      ui.visibilite("visible");
      ui.reseau(true);
      await ui.flush();
      console.log(JSON.stringify({ texte: ui.q("#ecran-lecture").textContent, timers: vivants(ui),
        relances: gets(ui) - apres, visibles: ui.visibles() }));
    """)
    assert "Télécharger" in r["texte"]  # écran « Vidéo prête » (player.js, #163)
    assert r["timers"] == 0
    assert r["relances"] == 0  # suivi terminé : plus aucun appel
    assert r["visibles"] == ["ecran-lecture"]


# --- Scénario : pas de doublon ---
@needs_node
def test_deux_retours_rapides_un_seul_appel_et_un_seul_minuteur() -> None:
    r = run("""
      const ui = demarrer({ routes });
      await lancer(ui);
      const avant = gets(ui);
      ui.visibilite("visible");
      ui.visibilite("visible");
      ui.reseau(true);
      await ui.flush();
      ui.visibilite("visible");
      await ui.flush();
      console.log(JSON.stringify({ appels: gets(ui) - avant, timers: vivants(ui) }));
    """)
    assert r["appels"] == 2  # un par réponse reçue : jamais deux en vol
    assert r["timers"] == 1


@needs_node
def test_rafale_pendant_la_requete_en_vol_un_seul_appel() -> None:
    r = run("""
      const ui = demarrer({ routes });
      await lancer(ui);
      const avant = gets(ui);
      ui.visibilite("visible");
      ui.visibilite("visible");
      await ui.flush();
      console.log(JSON.stringify({ appels: gets(ui) - avant, timers: vivants(ui) }));
    """)
    assert r["appels"] == 1
    assert r["timers"] == 1


# --- Scénario : hors ligne ---
@needs_node
def test_hors_ligne_au_retour_ecran_pas_de_connexion_sans_appel_ni_erreur_technique() -> None:
    r = run("""
      const ui = demarrer({ routes });
      await lancer(ui);
      coupe = true;
      ui.reseau(false);
      const avant = gets(ui);
      ui.visibilite("visible");
      await ui.flush();
      const hors = gets(ui) - avant;
      await ui.sonder(); // le minuteur déjà armé échoue : toujours pas d'écran « raté »
      const t = ui.q("#ecran-erreur").textContent + ui.q("#ecran-statut").textContent;
      console.log(JSON.stringify({ visibles: ui.visibles(), t, hors, timers: vivants(ui) }));
    """)
    assert r["visibles"] == ["ecran-erreur"]  # l'écran « pas de connexion » du shell
    assert r["hors"] == 0  # hors ligne : aucun appel au retour
    assert "Oups, pas de connexion" in r["t"]
    assert "raté" not in r["t"]
    assert not any(m in r["t"] for m in ("TypeError", "Failed", "http", "fetch"))
    assert r["timers"] == 0


@needs_node
def test_la_reconnexion_reprend_le_suivi() -> None:
    r = run("""
      const ui = demarrer({ routes });
      await lancer(ui);
      coupe = true;
      ui.reseau(false);
      ui.visibilite("visible");
      await ui.sonder();
      const coupeVisibles = ui.visibles();
      coupe = false;
      etat = { state: "running", step: "montage", progress: { pct: 90 } };
      ui.reseau(true);
      await ui.flush();
      console.log(JSON.stringify({ coupeVisibles, visibles: ui.visibles(), v: valeur(ui),
        timers: vivants(ui) }));
    """)
    assert r["coupeVisibles"] == ["ecran-erreur"]
    assert r["visibles"] == ["ecran-statut"]
    assert r["v"] == "90"
    assert r["timers"] == 1


# --- Panne serveur en ligne : écran « raté », Réessayer sans doublon ---
@needs_node
def test_reessayer_ignore_pendant_une_requete_en_vol() -> None:
    r = run("""
      const ui = demarrer({ routes });
      await lancer(ui);
      coupe = true;
      await ui.sonder(); // en ligne mais requête rejetée : écran d'erreur du suivi
      coupe = false;
      const avant = ui.appels.length; // GET et POST confondus : un renvoi serait un POST
      ui.bouton("Réessayer").click();
      ui.bouton("Réessayer").click();
      await ui.flush();
      console.log(JSON.stringify({ appels: ui.appels.length - avant, timers: vivants(ui),
        visibles: ui.visibles() }));
    """)
    assert r["appels"] == 1
    assert r["timers"] == 1
    assert r["visibles"] == ["ecran-statut"]


# --- Autre idée : le suivi s'arrête, une réponse tardive est ignorée ---
@needs_node
def test_apres_autre_idee_plus_d_appel_et_reponse_tardive_ignoree() -> None:
    r = run("""
      const ui = demarrer({ routes });
      await lancer(ui);
      coupe = true;
      await ui.sonder();
      coupe = false;
      ui.bouton("Réessayer").click(); // requête en vol...
      ui.bouton("Autre idée").click(); // ...l'enfant repart à l'accueil
      await ui.flush();
      const avant = gets(ui);
      ui.visibilite("visible");
      ui.reseau(true);
      await ui.flush();
      console.log(JSON.stringify({ visibles: ui.visibles(), relances: gets(ui) - avant,
        timers: vivants(ui) }));
    """)
    assert r["visibles"] == ["ecran-accueil"]  # la réponse tardive n'a pas rouvert le suivi
    assert r["relances"] == 0
    assert r["timers"] == 0


# --- Sans suivi actif, rien ne part ---
@needs_node
def test_sans_job_suivi_aucun_appel() -> None:
    r = run("""
      const ui = demarrer({ routes });
      ui.visibilite("visible");
      ui.reseau(true);
      await ui.flush();
      console.log(JSON.stringify({ appels: ui.appels.length }));
    """)
    assert r["appels"] == 0


def test_contrat_inchange_et_note_de_design() -> None:
    src = (Path(__file__).resolve().parents[1] / "static" / "status.js").read_text("utf-8")
    assert '"visibilitychange"' in src
    assert '"online"' in src
    assert "#233" in DOC.read_text("utf-8")
