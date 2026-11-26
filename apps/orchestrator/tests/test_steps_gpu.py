"""Étapes GPU (#245) : image (Flux) et clip (Wan 2.2) via un faux ComfyUI, sans GPU ni PC distant.

`FauxComfy` simule l'API (`/prompt`, `/history`, `/view`, `/upload/image`, `/free`) : en mémoire
(transport injecté) ou derrière un vrai serveur HTTP sur 127.0.0.1 (transport urllib réel).
"""

from __future__ import annotations

import contextlib
import http.server
import json
import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from orchestrator.steps_gpu import (
    ClientComfy,
    ErreurComfy,
    EtapesGpu,
    Requete,
    comfy_depuis_env,
    decharger_ollama,
)
from orchestrator.steps_llm import ErreurConfiguration, OllamaInjoignable

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
MP4 = b"\0\0\0\x18ftypisom" + b"\0" * 32
PROMPT = "a violet owl drums a secret tambourine on the moon"
MOTION = "the owl beats the drum slowly, camera pushes in"
URL = "http://127.0.0.1:8188"
OLLAMA = "http://ollama.test:11434"


class FauxComfy:
    """Fausse API ComfyUI ; `mode` : ok, refus, echec, jamais, hostile, renomme, corrompu.

    `cible` (SaveImage, SaveVideo ou None = tous) limite le mode aux workflows de cette sortie.
    """

    def __init__(
        self, journal: list[str] | None = None, mode: str = "ok", cible: str | None = None
    ) -> None:
        self.journal = [] if journal is None else journal
        self.mode = mode
        self.cible = cible
        self.workflows: list[dict[str, Any]] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.frees: list[Any] = []

    def repondre(self, methode: str, chemin: str, corps: bytes | None) -> tuple[int, bytes]:
        route = urlsplit(chemin).path
        self.journal.append(f"comfy {methode} {route}")
        if route == "/prompt":
            return self._prompt(json.loads(corps or b"{}"))
        if route.startswith("/history/"):
            return self._history(route.rsplit("/", 1)[1])
        if route == "/view":
            nom = parse_qs(urlsplit(chemin).query)["filename"][0]
            contenu = MP4 if nom.endswith(".mp4") else PNG
            sortie = "SaveVideo" if nom.endswith(".mp4") else "SaveImage"
            corrompu = self.mode == "corrompu" and self.cible in (None, sortie)
            return 200, b"pas un fichier" if corrompu else contenu
        if route == "/upload/image":
            return self._upload(corps or b"")
        if route == "/free":
            self.frees.append(json.loads(corps or b"{}"))
            return 200, b""
        return 404, b""

    def _prompt(self, corps: dict[str, Any]) -> tuple[int, bytes]:
        self.workflows.append(corps["prompt"])
        if self._mode(corps["prompt"]) == "refus":
            erreur = {"error": {"message": f"invalid prompt {json.dumps(corps)}"}}
            return 400, json.dumps(erreur).encode()
        reponse = {"prompt_id": f"p-{len(self.workflows)}", "number": 1, "node_errors": {}}
        return 200, json.dumps(reponse).encode()

    def _history(self, prompt_id: str) -> tuple[int, bytes]:
        wf = self.workflows[int(prompt_id.split("-")[1]) - 1]
        mode = self._mode(wf)
        if mode == "jamais":
            return 200, b"{}"
        video = any(n["class_type"] == "SaveVideo" for n in wf.values())
        noeud = next(k for k, n in wf.items() if n["class_type"] in ("SaveImage", "SaveVideo"))
        nom = "vva_clip_00001_.mp4" if video else "vva_00001_.png"
        if mode == "hostile":
            nom = "../../x.mp4"
        statut = {"status_str": "success", "completed": True, "messages": []}
        if mode == "echec":
            message = {"exception_message": f"CUDA out of memory for {json.dumps(wf)}"}
            statut = {"status_str": "error", "completed": False, "messages": [message]}
        sortie = {noeud: {"images": [{"filename": nom, "subfolder": "", "type": "output"}]}}
        entree = {"prompt": [1, prompt_id, wf], "outputs": sortie, "status": statut}
        return 200, json.dumps({prompt_id: entree}).encode()

    def _mode(self, wf: dict[str, Any]) -> str:
        sorties = {n["class_type"] for n in wf.values()}
        return self.mode if self.cible is None or self.cible in sorties else "ok"

    def _upload(self, corps: bytes) -> tuple[int, bytes]:
        trouve = re.search(rb'name="image"; filename="([^"]+)"\r\n[^\r]*\r\n\r\n', corps)
        assert trouve, "multipart sans champ image"
        nom = trouve.group(1).decode()
        debut = trouve.end()
        contenu = corps[debut : corps.index(b"\r\n--", debut)]
        self.uploads.append((nom, contenu))
        assert b'name="overwrite"\r\n\r\ntrue' in corps
        rendu = f"autre-{nom}" if self.mode == "renomme" else nom
        return 200, json.dumps({"name": rendu, "subfolder": "", "type": "input"}).encode()

    def transport(self, requete: Requete, timeout: float) -> bytes:
        """Transport en mémoire : aucune socket ; erreur HTTP = exception citant la réponse."""
        morceaux = urlsplit(requete.url)
        chemin = morceaux.path + (f"?{morceaux.query}" if morceaux.query else "")
        methode = "GET" if requete.corps is None else "POST"
        statut, corps = self.repondre(methode, chemin, requete.corps)
        if statut >= 400:
            raise OSError(f"HTTP {statut} {corps.decode()}")
        return corps


MODELES = ["qwen3:8b", "llava:7b"]


class FauxOllama:
    """Transport Ollama factice : `/api/ps` (modèles chargés) puis `/api/generate` keep_alive 0."""

    def __init__(self, journal: list[str], ps: bytes | None = None) -> None:
        self.journal = journal
        self.corps: list[dict[str, Any]] = []
        self.ps = ps or json.dumps({"models": [{"name": m} for m in MODELES]}).encode()

    def __call__(self, requete: Requete, timeout: float) -> bytes:
        methode = "GET" if requete.corps is None else "POST"
        self.journal.append(f"ollama {methode} {urlsplit(requete.url).path}")
        if requete.corps is None:
            return self.ps
        self.corps.append(json.loads(requete.corps))
        return b'{"done": true}'


class Horloge:
    """Temps factice : `dormir` avance l'horloge sans attendre."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def dormir(self, s: float) -> None:
        self.t += s


def _gpu(tmp_path: Path, mode: str = "ok", **kwargs: Any) -> tuple[EtapesGpu, FauxComfy, list[str]]:
    journal: list[str] = []
    faux = FauxComfy(journal, mode)
    horloge = Horloge()
    comfy = ClientComfy(URL, transport=faux.transport, horloge=horloge, dormir=horloge.dormir)
    decharge = decharger_ollama(OLLAMA, FauxOllama(journal))
    gpu = EtapesGpu(comfy, decharge, tmp_path / "out", **kwargs)
    return gpu, faux, journal


def _job(n: int = 1, **champs: Any) -> dict[str, Any]:
    job = {
        "schema_version": "1.0",
        "job_id": f"r245-clip-{n}",
        "type": "image_to_clip",
        "image_path": f"r245-img-{n}.png",
        "motion_prompt": MOTION,
        "frames": 49,
    }
    return {**job, **champs}


def _textes(wf: dict[str, Any]) -> list[Any]:
    return [v for n in wf.values() for v in n["inputs"].values() if isinstance(v, str)]


# Scénario: configuration refusée (boucle locale seulement, aucun appel réseau)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://192.168.1.14:8188",
        "http://comfy.test:8188",
        "http://127.0.0.1.evil.test:8188",
        "http://user@127.0.0.1:8188",
        "ftp://127.0.0.1:8188",
        "http://127.0.0.1:8188/api",
        "http://127.0.0.1:8188/?x=1",
        "http://127.0.0.1:99999",
        "http://0.0.0.0:8188",
    ],
)
def test_comfy_url_hors_boucle_locale_refusee(url: str) -> None:
    appels: list[Requete] = []
    with pytest.raises(ErreurConfiguration, match="COMFY_URL"):
        comfy_depuis_env({"COMFY_URL": url}, transport=lambda r, t: appels.append(r) or b"")
    assert appels == []


def test_comfy_url_absente_refusee() -> None:
    with pytest.raises(ErreurConfiguration, match="COMFY_URL"):
        comfy_depuis_env({})


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8188", "http://localhost:8188/", "http://[::1]:8188"]
)
def test_comfy_url_boucle_locale_acceptee(url: str) -> None:
    assert comfy_depuis_env({"COMFY_URL": url}).url == url.rstrip("/")


def test_workflow_sans_marqueur_erreur_de_configuration(tmp_path: Path) -> None:
    gpu_dir = tmp_path / "wf"
    gpu_dir.mkdir()
    (gpu_dir / "flux_schnell_t2i_api.json").write_text(
        json.dumps({"9": {"class_type": "SaveImage", "inputs": {}}}), encoding="utf-8"
    )
    with pytest.raises(ErreurConfiguration, match="workflow"):
        _gpu(tmp_path, workflows=gpu_dir)


# Scénario: dictée vers MP4 (image puis clip)


def test_image_rend_un_chemin_d_entree_comfyui(tmp_path: Path) -> None:
    gpu, faux, _ = _gpu(tmp_path)
    assert gpu.image("r245-img-1", PROMPT) == "r245-img-1.png"
    [wf] = faux.workflows
    assert PROMPT in _textes(wf)
    assert not any("{{" in t for t in _textes(wf))
    assert faux.uploads == [("r245-img-1.png", PNG)]


def test_clip_telecharge_le_mp4_dans_le_dossier_du_run(tmp_path: Path) -> None:
    gpu, faux, _ = _gpu(tmp_path)
    chemin = gpu.clip(_job())
    assert chemin == str(tmp_path / "out" / "r245" / "r245-clip-1.mp4")
    assert Path(chemin).read_bytes() == MP4
    [wf] = faux.workflows
    assert MOTION in _textes(wf)
    assert "r245-img-1.png" in _textes(wf)
    assert [n["inputs"]["length"] for n in wf.values() if "length" in n["inputs"]] == [49]
    assert not any("{{" in t for t in _textes(wf))


# Scénario: mémoire graphique


VRAM = ("comfy POST /free", "ollama POST /api/generate", "comfy POST /prompt")


def test_memoire_graphique_free_a_chaque_changement_de_phase(tmp_path: Path) -> None:
    gpu, faux, journal = _gpu(tmp_path)
    gpu.image("r245-img-1", PROMPT)
    gpu.image("r245-img-2", PROMPT)
    gpu.clip(_job(1))
    gpu.clip(_job(2))
    gpu.image("r246-img-1", PROMPT)  # run suivant : Wan ne doit pas rester en VRAM
    free, ollama, prompt = VRAM
    decharge = [ollama] * len(MODELES)
    attendu = [free, *decharge, prompt, *decharge, prompt, free, prompt, prompt]
    attendu += [free, *decharge, prompt]
    assert [ligne for ligne in journal if ligne in VRAM] == attendu
    assert faux.frees == [{"unload_models": True, "free_memory": True}] * 3


def test_avant_voix_libere_comfyui_et_decharge_ollama(tmp_path: Path) -> None:
    """#249 : SILMA tourne sur le GPU ; ni Flux, ni Wan, ni Ollama ne restent en VRAM."""
    gpu, faux, journal = _gpu(tmp_path)
    gpu.clip(_job(1))
    gpu.avant_voix()
    gpu.image("r246-img-1", PROMPT)  # après la voix : ComfyUI libéré de nouveau
    free, ollama, prompt = VRAM
    decharge = [ollama] * len(MODELES)
    attendu = [free, prompt, free, *decharge, free, *decharge, prompt]
    assert [ligne for ligne in journal if ligne in VRAM] == attendu


def test_dechargement_de_tous_les_modeles_listes_par_ps() -> None:
    journal: list[str] = []
    ollama = FauxOllama(journal)
    decharger_ollama(OLLAMA, ollama)()
    assert journal[0] == "ollama GET /api/ps"
    assert ollama.corps == [{"model": m, "keep_alive": 0} for m in MODELES]


@pytest.mark.parametrize(
    "ps",
    [
        b"{" + b" " * 2_000_000 + b"}",
        b"pas du json",
        json.dumps({"models": [{"name": "../x"}]}).encode(),
        json.dumps({"models": [{"name": f"m{i}"} for i in range(100)]}).encode(),
        json.dumps({"models": "qwen3"}).encode(),
    ],
)
def test_ps_hors_contrat_echec_ferme_sans_dechargement(ps: bytes) -> None:
    ollama = FauxOllama([], ps)
    with pytest.raises(OllamaInjoignable) as exc:
        decharger_ollama(OLLAMA, ollama)()
    assert exc.value.__context__ is None
    assert ollama.corps == []


def test_graine_derivee_du_job_id(tmp_path: Path) -> None:
    gpu, faux, _ = _gpu(tmp_path)
    gpu.image("r245-img-1", PROMPT)
    gpu.image("r245-img-2", PROMPT)
    gpu.image("r245-img-1", PROMPT)
    gpu.clip(_job(1))
    graines = [
        n["inputs"]["seed"] for wf in faux.workflows for n in wf.values() if "seed" in n["inputs"]
    ]
    assert len(graines) == 4
    assert graines[0] == graines[2]
    assert len({graines[0], graines[1], graines[3]}) == 3
    assert all(type(g) is int and 0 <= g < 2**32 for g in graines)


def test_dechargement_ollama_en_echec_aucun_rendu(tmp_path: Path) -> None:
    faux = FauxComfy()

    def panne(requete: Requete, timeout: float) -> bytes:
        raise OSError(PROMPT)

    comfy = ClientComfy(URL, transport=faux.transport)
    gpu = EtapesGpu(comfy, decharger_ollama(OLLAMA, panne), tmp_path)
    with pytest.raises(OllamaInjoignable) as exc:
        gpu.image("r245-img-1", PROMPT)
    assert exc.value.__context__ is None
    assert "owl" not in str(exc.value)
    assert "comfy POST /prompt" not in faux.journal


# Scénario: rendu en échec ou trop long (sans fuite du prompt)


@pytest.mark.parametrize("mode", ["refus", "echec", "jamais", "corrompu"])
@pytest.mark.parametrize("etape", ["image", "clip"])
def test_rendu_en_echec_sans_fuite(
    tmp_path: Path, mode: str, etape: str, caplog: pytest.LogCaptureFixture
) -> None:
    gpu, _, _ = _gpu(tmp_path, mode)
    with caplog.at_level(logging.DEBUG), pytest.raises(ErreurComfy) as exc:
        gpu.image("r245-img-1", PROMPT) if etape == "image" else gpu.clip(_job())
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    for texte in (str(exc.value), caplog.text):
        assert "owl" not in texte
        assert "drum" not in texte
    assert not list(tmp_path.rglob("*.mp4"))


def test_delai_depasse_borne(tmp_path: Path) -> None:
    gpu, faux, _ = _gpu(tmp_path, "jamais")
    with pytest.raises(ErreurComfy, match="délai"):
        gpu.image("r245-img-1", PROMPT)
    assert 1 < faux.journal.count("comfy GET /history/p-1") < 10_000


def test_image_renommee_par_comfyui_refusee(tmp_path: Path) -> None:
    gpu, _, _ = _gpu(tmp_path, "renomme")
    with pytest.raises(ErreurComfy):
        gpu.image("r245-img-1", PROMPT)


def test_reponse_trop_grande_refusee(tmp_path: Path) -> None:
    gpu, faux, _ = _gpu(tmp_path)
    original = faux.repondre

    def gonfle(methode: str, chemin: str, corps: bytes | None) -> tuple[int, bytes]:
        statut, contenu = original(methode, chemin, corps)
        return (statut, contenu + b" " * 2_000_000) if "/history/" in chemin else (statut, contenu)

    faux.repondre = gonfle  # type: ignore[method-assign]
    with pytest.raises(ErreurComfy):
        gpu.image("r245-img-1", PROMPT)


# Scénario: nom de fichier hostile


def test_nom_de_fichier_hostile_rien_hors_du_run(tmp_path: Path) -> None:
    gpu, faux, _ = _gpu(tmp_path, "hostile")
    with pytest.raises(ErreurComfy):
        gpu.clip(_job())
    assert not list(tmp_path.rglob("x.mp4"))
    assert not list(tmp_path.rglob("*.mp4"))
    assert not any("/view" in ligne for ligne in faux.journal)


def test_dossier_du_run_en_lien_symbolique_refuse(tmp_path: Path) -> None:
    gpu, _, _ = _gpu(tmp_path)
    ailleurs = tmp_path / "ailleurs"
    ailleurs.mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "r245").symlink_to(ailleurs)
    with pytest.raises(ErreurComfy):
        gpu.clip(_job())
    assert list(ailleurs.iterdir()) == []


@pytest.mark.parametrize(
    "champs",
    [
        {"frames": 50},
        {"frames": True},
        {"image_path": "../x.png"},
        {"job_id": "../../r-clip-1"},
        {"job_id": "r245-img-1"},
        {"motion_prompt": ""},
        {"type": "autre"},
    ],
)
def test_gpu_job_hors_contrat_refuse_sans_appel(tmp_path: Path, champs: dict[str, Any]) -> None:
    gpu, faux, _ = _gpu(tmp_path)
    with pytest.raises(ErreurComfy):
        gpu.clip(_job(**champs))
    assert faux.journal == []


# Transport réel (urllib) contre un faux ComfyUI HTTP sur 127.0.0.1


class ServeurFaux(http.server.ThreadingHTTPServer):
    """Sert une application `repondre(methode, chemin, corps)` ; `pause_s` retarde les réponses."""

    def __init__(self, app: Callable[[str, str, bytes | None], tuple[int, bytes]]) -> None:
        self.app = app
        self.pause_s = 0.0
        self.redirection: str | None = None
        self.goutte_s = 0.0
        super().__init__(("127.0.0.1", 0), _Gestionnaire)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Gestionnaire(http.server.BaseHTTPRequestHandler):
    server: ServeurFaux

    def _repondre(self) -> None:
        longueur = int(self.headers.get("Content-Length") or 0)
        corps = self.rfile.read(longueur) if self.command == "POST" else None
        time.sleep(self.server.pause_s)
        if self.server.redirection:
            self.send_response(302)
            self.send_header("Location", self.server.redirection + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        statut, contenu = self.server.app(self.command, self.path, corps)
        self.send_response(statut)
        self.send_header("Content-Length", str(len(contenu)))
        self.end_headers()
        if self.server.goutte_s and self.path.startswith("/view"):
            for octet in contenu:  # compte-gouttes : chaque lecture socket reste sous le délai
                self.wfile.write(bytes([octet]))
                self.wfile.flush()
                time.sleep(self.server.goutte_s)
            return
        self.wfile.write(contenu)

    do_GET = do_POST = _repondre

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def serveurs() -> Iterator[Callable[[Callable[..., tuple[int, bytes]]], ServeurFaux]]:
    lances: list[ServeurFaux] = []

    def lancer(app: Callable[..., tuple[int, bytes]]) -> ServeurFaux:
        serveur = ServeurFaux(app)
        threading.Thread(target=serveur.serve_forever, daemon=True).start()
        lances.append(serveur)
        return serveur

    yield lancer
    for serveur in lances:
        serveur.shutdown()
        serveur.server_close()


def _gpu_http(tmp_path: Path, url: str, **kwargs: Any) -> tuple[EtapesGpu, list[str]]:
    journal: list[str] = []
    gpu_kwargs = {k: kwargs.pop(k) for k in ("delai_image_s", "delai_clip_s") if k in kwargs}
    comfy = ClientComfy(url, **kwargs)
    decharge = decharger_ollama(OLLAMA, FauxOllama(journal))
    return EtapesGpu(comfy, decharge, tmp_path / "out", **gpu_kwargs), journal


def test_http_reel_image_et_clip(tmp_path: Path, serveurs: Any) -> None:
    faux = FauxComfy()
    serveur = serveurs(faux.repondre)
    gpu, _ = _gpu_http(tmp_path, serveur.url)
    assert gpu.image("r245-img-1", PROMPT) == "r245-img-1.png"
    assert Path(gpu.clip(_job())).read_bytes() == MP4
    assert faux.uploads == [("r245-img-1.png", PNG)]


def test_http_redirection_non_suivie(tmp_path: Path, serveurs: Any) -> None:
    cible = FauxComfy()
    serveur_cible = serveurs(cible.repondre)
    serveur = serveurs(FauxComfy().repondre)
    serveur.redirection = serveur_cible.url
    gpu, _ = _gpu_http(tmp_path, serveur.url)
    with pytest.raises(ErreurComfy):
        gpu.image("r245-img-1", PROMPT)
    assert cible.journal == []


def test_http_proxy_ignore(tmp_path: Path, serveurs: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = FauxComfy()
    serveur_proxy = serveurs(proxy.repondre)
    for nom in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(nom, raising=False)
    for nom in ("HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(nom, serveur_proxy.url)
    faux = FauxComfy()
    gpu, _ = _gpu_http(tmp_path, serveurs(faux.repondre).url)
    assert gpu.image("r245-img-1", PROMPT) == "r245-img-1.png"
    assert proxy.journal == []


def test_http_serveur_muet_delai_borne(tmp_path: Path, serveurs: Any) -> None:
    serveur = serveurs(FauxComfy().repondre)
    serveur.pause_s = 1.0
    gpu, _ = _gpu_http(tmp_path, serveur.url, timeout_s=0.2)
    debut = time.monotonic()
    with pytest.raises(ErreurComfy):
        gpu.image("r245-img-1", PROMPT)
    assert time.monotonic() - debut < 0.9


def test_http_view_au_compte_gouttes_borne_par_le_delai_de_l_etape(
    tmp_path: Path, serveurs: Any
) -> None:
    serveur = serveurs(FauxComfy().repondre)
    serveur.goutte_s = 0.05  # 41 octets : ~2 s au total, chaque octet bien sous timeout_s
    gpu, _ = _gpu_http(tmp_path, serveur.url, timeout_s=0.5, delai_image_s=0.6)
    debut = time.monotonic()
    with pytest.raises(ErreurComfy):
        gpu.image("r245-img-1", PROMPT)
    assert time.monotonic() - debut < 1.5


# QA #245 : cas limites du correctif f827efe (reprise après panne, /api/ps vide, darija)


def test_ps_sans_modele_charge_aucun_dechargement() -> None:
    ollama = FauxOllama([], b'{"models": []}')
    decharger_ollama(OLLAMA, ollama)()
    assert ollama.corps == []


def test_free_en_panne_aucun_rendu_puis_free_rejoue(tmp_path: Path) -> None:
    gpu, faux, journal = _gpu(tmp_path)
    original = faux.repondre
    pannes = [True]

    def repondre(methode: str, chemin: str, corps: bytes | None) -> tuple[int, bytes]:
        if chemin == "/free" and pannes:
            pannes.pop()
            return 500, b""
        return original(methode, chemin, corps)

    faux.repondre = repondre  # type: ignore[method-assign]
    with pytest.raises(ErreurComfy):
        gpu.clip(_job())
    assert "comfy POST /prompt" not in journal
    assert Path(gpu.clip(_job())).read_bytes() == MP4
    assert journal.index("comfy POST /free") < journal.index("comfy POST /prompt")


def test_prompt_en_darija_transmis_intact_et_sans_fuite(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    darija = "البومة كتلعب بالطبل فوق القمر"
    gpu, faux, _ = _gpu(tmp_path)
    gpu.image("r245-img-1", darija)
    assert darija in _textes(faux.workflows[0])
    faux.mode = "echec"
    with caplog.at_level(logging.DEBUG), pytest.raises(ErreurComfy) as exc:
        gpu.image("r245-img-2", darija)
    for texte in (str(exc.value), caplog.text):
        assert "البومة" not in texte
        assert "\\u0627" not in texte


# QA #245 : transport réel, corps tronqué refusé et en-têtes au compte-gouttes bornés


def _serveur_brut(envoyer: Callable[[socket.socket], None]) -> str:
    """Serveur d'une seule connexion qui écrit des octets HTTP arbitraires."""
    ecoute = socket.create_server(("127.0.0.1", 0))

    def servir() -> None:
        with ecoute:
            connexion, _ = ecoute.accept()
        with connexion, contextlib.suppress(OSError):
            connexion.recv(65_536)
            envoyer(connexion)

    threading.Thread(target=servir, daemon=True).start()
    return f"http://127.0.0.1:{ecoute.getsockname()[1]}"


def test_http_reponse_tronquee_refusee() -> None:
    def tronquer(connexion: socket.socket) -> None:
        connexion.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + MP4)

    comfy = ClientComfy(_serveur_brut(tronquer), timeout_s=1.0)
    with pytest.raises(ErreurComfy):
        comfy.envoyer(Requete("/view", limite=10_000))


def test_http_en_tetes_au_compte_gouttes_bornes() -> None:
    def goutter(connexion: socket.socket) -> None:
        for octet in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
            connexion.sendall(bytes([octet]))
            time.sleep(0.04)  # ~1,5 s au total, chaque lecture sous le délai

    comfy = ClientComfy(_serveur_brut(goutter), timeout_s=0.2)
    debut = time.monotonic()
    with contextlib.suppress(ErreurComfy):
        comfy.envoyer(Requete("/history/x"))
    assert time.monotonic() - debut < 0.8
