"""Messages d'erreur de dictée adaptés à l'enfant (#234) : 3 cas distincts + repli générique.

Les vrais scripts de static/ tournent sous node dans le mini DOM (tests/minidom.js) ; fetch et micro
sont simulés. Textes synthétiques : rien d'un enfant ni de technique ne doit apparaître à l'écran.
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
GENERIQUE = ("Oups, je n'ai pas compris", "Mimo n'a pas réussi à t'écouter. Réessaie.", "Dictée")

RESEAU = "reseau"  # fetch rejeté (pas de réponse HTTP)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node absent")


def ecran_erreur(reponse: str) -> dict[str, Any]:
    """Dicte avec `reponse` (JS : { status, corps } ou throw) et lit l'écran Erreur affiché."""
    code = f"""
const {{ demarrer }} = require({json.dumps(str(MINIDOM))});
(async () => {{
  const ui = demarrer({{ routes: (url) => {{
    if (url === "/api/dictation") {{ {reponse} }}
  }} }});
  if ({json.dumps(reponse == RESEAU)}) ui.win.fetch = () => Promise.reject(new Error("réseau"));
  await ui.dicter();
  const e = ui.q("#ecran-erreur");
  const retry = ui.bouton("Réessayer");
  console.log(JSON.stringify({{
    visibles: ui.visibles(),
    titre: e.querySelector("h2").textContent,
    detail: e.querySelector("p").textContent,
    puce: e.querySelector(".kid-chip").textContent.trim(),
    texte: e.textContent,
    reessayer: !!retry && retry.getAttribute("disabled") === null,
  }}));
}})().catch((x) => {{ console.error(x); process.exit(1); }});
"""
    out = subprocess.run(  # noqa: S603
        ["node", "-e", code],  # noqa: S607
        capture_output=True, text=True, check=False, timeout=20,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    r: dict[str, Any] = json.loads(out.stdout)
    return r


def triplet(r: dict[str, Any]) -> tuple[str, str, str]:
    return r["titre"], r["detail"], r["puce"]


@needs_node
def test_rien_entendu_texte_vide() -> None:
    r = ecran_erreur('return { status: 200, corps: { text: "" } };')
    assert r["visibles"] == ["ecran-erreur"]
    assert triplet(r) == ("Je n'ai rien entendu", "Parle un peu plus fort, puis réessaie", "Dictée")


@needs_node
def test_rien_entendu_texte_blanc_seulement() -> None:
    r = ecran_erreur('return { status: 200, corps: { text: "   " } };')
    assert r["titre"] == "Je n'ai rien entendu"


@needs_node
def test_dictee_trop_longue_413() -> None:
    r = ecran_erreur('return { status: 413, corps: { error: "audio_trop_gros" } };')
    assert r["visibles"] == ["ecran-erreur"]
    assert triplet(r) == ("C'est un peu long", "Dis-moi une idée plus courte", "Dictée")


@needs_node
def test_ecoute_indisponible_503_reessayer_reste_disponible() -> None:
    r = ecran_erreur('return { status: 503, corps: { error: "asr_indisponible" } };')
    assert r["visibles"] == ["ecran-erreur"]
    assert triplet(r) == ("Mimo ne peut pas écouter", "Demande à un grand de m'aider", "Dictée")
    assert r["reessayer"] is True


@needs_node
@pytest.mark.parametrize(
    "reponse",
    [
        'return { status: 500, corps: { error: "transcription_echouee" } };',
        'return { status: 400, corps: { error: "audio_vide" } };',
        'return { status: 415, corps: { error: "type_non_supporte" } };',
        'return { status: 503, corps: { error: "autre_chose" } };',  # 503 hors contrat connu
        "return { status: 200, corps: { text: 42 } };",  # corps hors contrat
        "return { status: 200, corps: null };",
        RESEAU,
    ],
)
def test_autre_echec_garde_le_message_generique(reponse: str) -> None:
    assert triplet(ecran_erreur(reponse)) == GENERIQUE


@needs_node
def test_aucune_fuite_texte_dicte_ni_code_technique() -> None:
    secret = "mon-secret-d-enfant"  # noqa: S105 - texte synthétique
    cas = [
        f'return {{ status: 413, corps: {{ error: "audio_trop_gros", text: "{secret}" }} }};',
        f'return {{ status: 503, corps: {{ error: "asr_indisponible", text: "{secret}" }} }};',
        f'return {{ status: 500, corps: {{ error: "transcription_echouee", text: "{secret}" }} }};',
        'return { status: 200, corps: { text: "", error: "audio_vide" } };',
    ]
    for reponse in cas:
        texte = ecran_erreur(reponse)["texte"]
        assert secret not in texte
        assert not re.search(r"audio_|asr_|transcription_|[a-z]+_[a-z]+", texte)


def test_dictation_js_ne_journalise_rien() -> None:
    js = (STATIC / "dictation.js").read_text("utf-8")
    assert "console." not in js
