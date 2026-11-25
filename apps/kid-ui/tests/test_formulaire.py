"""Formulaire enfant de bout en bout (#214) : micro, texte validé, progression, lecteur.

Les vrais scripts de static/ tournent sous node dans un mini DOM (tests/minidom.js) ; fetch, micro
et minuteurs sont simulés : aucun navigateur, aucun serveur, aucun GPU. Textes synthétiques.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import server

STATIC = Path(server.STATIC_DIR)
MINIDOM = Path(__file__).resolve().parent / "minidom.js"
DICTEE = "Un dragon bleu qui fait du vélo"  # synthétique
JOB = "0123abcd-0123-4abc-8abc-0123456789ab"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node absent")

PRELUDE = f"""
const {{ demarrer }} = require({json.dumps(str(MINIDOM))});
const DICTEE = {json.dumps(DICTEE)};
const JOB = {json.dumps(JOB)};
const enCours = (pct) => ({{ state: "running", step: "images",
  progress: {{ pct, restant_s: 100, bientot: false }} }});
// etats : réponses successives de GET /api/jobs/<id> ; texte : réponse de /api/dictation.
function routes(etats, texte) {{
  return (url, init) => {{
    if (url === "/api/dictation") return {{ status: 200, corps: {{ text: texte || DICTEE }} }};
    if (url === "/api/jobs" && init.method === "POST")
      return {{ status: 202, corps: {{ job_id: JOB }} }};
    if (url === "/api/jobs/" + JOB) return {{ status: 200, corps: etats.shift() }};
  }};
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


# --- Scénario : la transcription s'affiche, rien n'est envoyé avant ✅ ---
@needs_node
def test_le_texte_dicte_s_affiche_avec_ok_et_rien_n_est_envoye() -> None:
    r = run("""
      const ui = demarrer({ routes: routes([]) });
      await ui.dicter();
      console.log(JSON.stringify({
        heard: ui.q(".heard").textContent,
        ok: !!ui.bouton("✅"),
        urls: ui.appels.map((a) => a.url),
        visibles: ui.visibles(),
      }));
    """)
    assert r["heard"] == DICTEE
    assert r["ok"] is True
    assert r["urls"] == ["/api/dictation"]  # pas de POST /api/jobs avant le clic
    assert r["visibles"] == ["kid-screen"]  # l'écran d'écoute seul


@needs_node
def test_le_texte_est_borne_au_contrat_des_jobs() -> None:
    r = run("""
      const ui = demarrer({ routes: routes([], "a".repeat(2000)) });
      await ui.dicter();
      ui.bouton("✅").click();
      await ui.flush();
      console.log(JSON.stringify(ui.appels.find((a) => a.url === "/api/jobs").json.text.length));
    """)
    assert r == 500


# --- Scénario : ✅ envoie le texte et la progression s'affiche ---
@needs_node
def test_ok_envoie_le_texte_et_affiche_la_progression() -> None:
    r = run("""
      const ui = demarrer({ routes: routes([enCours(40)]) });
      await ui.dicter();
      ui.bouton("✅").click();
      await ui.flush();
      await ui.sonder();
      const post = ui.appels.find((a) => a.url === "/api/jobs");
      console.log(JSON.stringify({
        post: { method: post.method, json: post.json, type: post.headers["Content-Type"] },
        visibles: ui.visibles(),
        h1: ui.q("#ecran-statut h1").textContent,
        now: ui.q(".kid-seg").getAttribute("aria-valuenow"),
        ecoute: ui.qa("[aria-labelledby=h-ecoute]").length,
        affiche: ui.q("#ecran-statut").textContent.includes("dragon"),
      }));
    """)
    assert r["post"] == {"method": "POST", "json": {"text": DICTEE}, "type": "application/json"}
    assert r["visibles"] == ["ecran-statut"]
    assert (r["h1"], r["now"], r["ecoute"]) == ("Ta vidéo arrive", "40", 0)
    assert r["affiche"] is False  # le texte de l'enfant n'est jamais réaffiché pendant l'attente


# --- Scénario : done -> mimo:video-prete -> lecteur ---
@needs_node
def test_done_emet_video_prete_et_affiche_le_lecteur() -> None:
    r = run("""
      const ui = demarrer({ routes: routes([enCours(50), { state: "done", step: "montage" }]) });
      const evts = [];
      ui.doc.addEventListener("mimo:video-prete", (e) => evts.push(e.detail.job_id));
      await ui.dicter();
      ui.bouton("✅").click();
      await ui.flush();
      await ui.sonder();
      const avant = evts.length;
      await ui.sonder();
      const v = ui.q("#ecran-lecture video");
      console.log(JSON.stringify({ avant, evts, visibles: ui.visibles(),
        src: v && v.getAttribute("src") }));
    """)
    assert r["avant"] == 0
    assert r["evts"] == [JOB]
    assert r["visibles"] == ["ecran-lecture"]
    assert r["src"] == f"/api/jobs/{JOB}/video"


# --- Scénario : erreur -> Réessayer (même texte) / Autre idée (retour à la dictée) ---
@needs_node
def test_erreur_propose_reessayer_et_autre_idee_reessayer_renvoie_le_meme_texte() -> None:
    r = run("""
      const ui = demarrer({ routes: routes([{ state: "error", step: "voix" },
        enCours(10)]) });
      await ui.dicter();
      ui.bouton("✅").click();
      await ui.flush();
      await ui.sonder();
      const boutons = ui.qa("#ecran-statut button").map((b) => b.textContent);
      ui.bouton("Réessayer").click();
      await ui.flush();
      const posts = ui.appels.filter((a) => a.url === "/api/jobs").map((a) => a.json);
      console.log(JSON.stringify({ boutons, posts, visibles: ui.visibles() }));
    """)
    assert r["boutons"] == ["Autre idée", "Réessayer"]
    assert r["posts"] == [{"text": DICTEE}, {"text": DICTEE}]
    assert r["visibles"] == ["ecran-statut"]


@needs_node
def test_autre_idee_revient_a_la_dictee_sans_rien_envoyer() -> None:
    """Retour à l'accueil (micro au focus) ; le texte est oublié et rien ne repart."""
    r = run("""
      const ui = demarrer({ routes: routes([{ state: "error", step: "voix" }]) });
      await ui.dicter();
      ui.bouton("✅").click();
      await ui.flush();
      await ui.sonder();
      const avant = ui.appels.length;
      ui.bouton("Autre idée").click();
      await ui.flush();
      const micro = ui.q('.kid-composer [aria-label="Parler"]');
      console.log(JSON.stringify({ visibles: ui.visibles(), focus: ui.doc.activeElement === micro,
        envoye: ui.appels.length - avant }));
    """)
    assert r == {"visibles": ["ecran-accueil"], "focus": True, "envoye": 0}


@needs_node
def test_dictee_bloquee_reessayer_inactif_autre_idee_proposee() -> None:
    r = run("""
      const ui = demarrer({ routes: routes([
        { state: "error", step: "storyboard", blocked: true }]) });
      await ui.dicter();
      ui.bouton("✅").click();
      await ui.flush();
      await ui.sonder();
      console.log(JSON.stringify({
        autre: !!ui.bouton("Autre idée"),
        inactif: ui.bouton("Réessayer").getAttribute("disabled") !== null,
      }));
    """)
    assert r == {"autre": True, "inactif": True}


# --- Correctif : « online » ne réaffiche pas l'accueil pendant la progression ---
@needs_node
def test_online_ne_reaffiche_pas_l_accueil_pendant_la_progression() -> None:
    r = run("""
      const ui = demarrer({ routes: routes([enCours(30), enCours(35)]) });
      await ui.dicter();
      ui.bouton("✅").click();
      await ui.flush();
      await ui.sonder();
      ui.reseau(true);  // événement « online » sans coupure : rien ne change
      const a = ui.visibles();
      ui.reseau(false); // coupure : écran « pas de connexion »
      const b = ui.visibles();
      ui.reseau(true);  // retour : on retrouve la progression, pas l'accueil
      console.log(JSON.stringify({ a, b, c: ui.visibles() }));
    """)
    assert r == {"a": ["ecran-statut"], "b": ["ecran-erreur"], "c": ["ecran-statut"]}


@needs_node
def test_coupure_depuis_l_accueil_puis_retour() -> None:
    r = run("""
      const ui = demarrer();
      const a = ui.visibles();
      ui.reseau(false);
      const b = ui.visibles();
      ui.reseau(true);
      console.log(JSON.stringify({ a, b, c: ui.visibles() }));
    """)
    assert r == {"a": ["ecran-accueil"], "b": ["ecran-erreur"], "c": ["ecran-accueil"]}


@needs_node
def test_demarrage_hors_ligne_puis_retour() -> None:
    r = run("""
      const ui = demarrer({ enLigne: false });
      const a = ui.visibles();
      ui.reseau(true);
      console.log(JSON.stringify({ a, b: ui.visibles() }));
    """)
    assert r == {"a": ["ecran-erreur"], "b": ["ecran-accueil"]}


@needs_node
def test_online_pendant_l_ecoute_revient_a_l_accueil() -> None:
    r = run("""
      const ui = demarrer({ routes: routes([]) });
      ui.q('.kid-composer [aria-label="Parler"]').click();
      await ui.flush();
      const a = ui.visibles();
      ui.reseau(true);
      console.log(JSON.stringify({ a, b: ui.visibles() }));
    """)
    assert r["a"] == ["kid-screen"]
    assert r["b"] == ["ecran-accueil"]


@needs_node
def test_coupure_pendant_la_lecture_met_la_video_en_pause_puis_la_rend() -> None:
    r = run("""
      const ui = demarrer({ routes: routes([{ state: "done", step: "montage" }]) });
      await ui.dicter();
      ui.bouton("✅").click();
      await ui.flush();
      await ui.sonder();
      ui.reseau(false);
      const b = ui.visibles();
      const pause = ui.q("video").paused;
      ui.reseau(true);
      console.log(JSON.stringify({ b, pause, c: ui.visibles() }));
    """)
    assert r == {"b": ["ecran-erreur"], "pause": True, "c": ["ecran-lecture"]}


# --- Tests statiques ---
def test_la_maquette_du_mini_dom_correspond_a_index_html() -> None:
    html = (STATIC / "index.html").read_text("utf-8")
    for motif in ('id="ecran-accueil"', 'id="ecran-erreur"', 'id="idee"', 'aria-label="Parler"',
                  'class="kid-composer"', 'class="kid-theme"', 'class="kid-chip bad"'):  # fmt: skip
        assert motif in html, motif
    ordre = re.findall(r'<script src="/([\w.]+\.js)"></script>', html)
    assert ordre[-4:] == ["status.js", "player.js", "shell.js", "dictation.js"]


def test_dictation_envoie_par_mimostatus_sans_innerhtml() -> None:
    js = (STATIC / "dictation.js").read_text("utf-8")
    assert "MimoStatus.envoyer(texte)" in js
    assert "idee.value" not in js  # le texte ne passe plus par le champ de saisie
    assert "innerHTML" not in js


def test_shell_ne_force_plus_l_accueil_en_ligne() -> None:
    js = (STATIC / "shell.js").read_text("utf-8")
    assert "accueil.hidden = !enLigne" not in js
    assert "if (erreur.hidden) return;" in js
