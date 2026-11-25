"""Étapes GPU réelles : image (Flux schnell) et clip (Wan 2.2 5B) via l'API ComfyUI (#245).

`COMFY_URL` vise la boucle locale seulement (127.0.0.1, localhost, ::1 : extrémité d'un tunnel
ouvert à la main) ; absente ou autre hôte : `ErreurConfiguration` avant tout appel réseau.
Durcissement de `steps_llm` : proxy ignoré, redirection refusée, délais bornés, réponses bornées
en taille. Les noms de fichiers rendus par ComfyUI ne servent qu'à `/view` (validés), jamais de
chemin local : le clip est écrit sous `<out>/<run_id>/<job_id>.mp4`. VRAM 8 Go : Ollama déchargé
avant chaque image, `/free` avant la phase clip. Échec : `ErreurComfy`, message générique, sans
prompt ni texte de ComfyUI, levée hors de tout bloc `except` (ni `__cause__` ni `__context__`).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from orchestrator.steps_llm import (
    RACINE,
    ClientOllama,
    ErreurConfiguration,
    OllamaInjoignable,
    _SansRedirection,
)

log = logging.getLogger(__name__)

WORKFLOWS = RACINE / "comfyui" / "workflows"
FLUX = "flux_schnell_t2i_api.json"
WAN = "wan22_5b_i2v_api.json"
HOTES_LOCAUX = frozenset({"localhost"})
TIMEOUT_S = 30.0  # par requête HTTP
DELAI_IMAGE_S = 600.0  # rendu d'une image, attente bornée
DELAI_CLIP_S = 1800.0  # rendu d'un clip
INTERVALLE_S = 1.0  # entre deux lectures de /history
TAILLE_JSON = 1_000_000
TAILLE_IMAGE = 50_000_000
TAILLE_CLIP = 200_000_000
FRAMES = frozenset(range(5, 122, 4))  # 4n+1, gpu-job.schema.json
_JOB_CLIP = re.compile(r"([A-Za-z0-9_-]{1,48})-clip-[0-9]{1,4}")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_IMAGE_PATH = re.compile(r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}")
_PROMPT_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_NOM_SORTIE = re.compile(r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_SOUS_DOSSIER = re.compile(r"(?:[A-Za-z0-9_-]{1,64}(?:/[A-Za-z0-9_-]{1,64}){0,3})?")


class ErreurComfy(Exception):
    """ComfyUI injoignable, réponse hors contrat, rendu en échec ou trop long (sans texte)."""


@dataclass(frozen=True)
class Requete:
    """Requête HTTP : GET si `corps` est None, sinon POST ; au plus `limite` octets lus."""

    url: str
    corps: bytes | None = None
    type_contenu: str = "application/json"
    limite: int = TAILLE_JSON


Transport = Callable[[Requete, float], bytes]

# Ni proxy d'environnement ni redirection : la requête ne va qu'à COMFY_URL.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SansRedirection())


def _http(requete: Requete, timeout: float) -> bytes:
    entetes = {} if requete.corps is None else {"Content-Type": requete.type_contenu}
    # S310 : schéma http et hôte de boucle locale imposés par ClientComfy.__post_init__.
    req = urllib.request.Request(requete.url, requete.corps, entetes)  # noqa: S310
    with _OPENER.open(req, timeout=timeout) as reponse:
        brut: bytes = reponse.read(requete.limite + 1)
    return brut


def _url_locale(url: str) -> bool:
    try:
        morceaux = urllib.parse.urlsplit(url)
        _ = morceaux.port  # ValueError si le port est invalide
    except ValueError:
        return False
    hote = morceaux.hostname or ""
    propre = morceaux.scheme == "http" and not (morceaux.username or morceaux.password)
    propre = propre and morceaux.path in ("", "/") and not (morceaux.query or morceaux.fragment)
    return propre and (hote in HOTES_LOCAUX or _ip_boucle(hote))


def _ip_boucle(hote: str) -> bool:
    try:
        ip = ipaddress.ip_address(hote)
    except ValueError:
        return False
    return str(ip) in ("127.0.0.1", "::1")


@dataclass(frozen=True)
class ClientComfy:
    """Client de l'API ComfyUI ; transport, horloge et attente injectables (faux en test)."""

    url: str
    transport: Transport = _http
    timeout_s: float = TIMEOUT_S
    intervalle_s: float = INTERVALLE_S
    horloge: Callable[[], float] = time.monotonic
    dormir: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if not _url_locale(self.url):
            raise ErreurConfiguration(
                "COMFY_URL doit viser la boucle locale (127.0.0.1, localhost, ::1)"
            )
        object.__setattr__(self, "url", self.url.rstrip("/"))

    def envoyer(self, requete: Requete) -> bytes:
        """Octets de la réponse ; toute panne ou réponse trop grande lève `ErreurComfy`."""
        complete = Requete(
            self.url + requete.url, requete.corps, requete.type_contenu, requete.limite
        )
        try:
            brut = self.transport(complete, self.timeout_s)
        except Exception as exc:  # toute panne de transport : échec fermé, type seul journalisé
            nom = type(exc).__name__
        else:
            if len(brut) <= requete.limite:
                return brut
            nom = "ReponseTropGrande"
        log.warning("comfyui requete en echec type=%s", nom)
        raise ErreurComfy("comfyui : requête en échec")

    def json(self, chemin: str, corps: Any = None) -> dict[str, Any]:
        donnees = None if corps is None else json.dumps(corps).encode()
        return _objet(self.envoyer(Requete(chemin, donnees)))

    def rendre(self, workflow: Mapping[str, Any], noeud: str, delai_s: float, limite: int) -> bytes:
        """Soumet le workflow, attend sa fin (`delai_s` au plus), rend la sortie de `noeud`."""
        soumis = self.json("/prompt", {"prompt": workflow, "client_id": uuid.uuid4().hex})
        prompt_id = soumis.get("prompt_id")
        if not isinstance(prompt_id, str) or not _PROMPT_ID.fullmatch(prompt_id):
            _echec("prompt_id hors contrat")
        sorties = self._attendre(prompt_id, delai_s)
        requete = urllib.parse.urlencode(_fichier(sorties.get(noeud)))
        return self.envoyer(Requete(f"/view?{requete}", limite=limite))

    def _attendre(self, prompt_id: str, delai_s: float) -> dict[str, Any]:
        fin = self.horloge() + delai_s
        while True:
            entree = self.json(f"/history/{prompt_id}").get(prompt_id)
            statut = entree.get("status") if isinstance(entree, dict) else None
            if isinstance(statut, dict) and statut.get("status_str") == "error":
                _echec("rendu en échec")
            if isinstance(statut, dict) and statut.get("completed") is True:
                sorties = entree.get("outputs") if isinstance(entree, dict) else None
                return sorties if isinstance(sorties, dict) else {}
            if self.horloge() >= fin:
                _echec("délai dépassé")
            self.dormir(self.intervalle_s)

    def liberer(self) -> None:
        corps = json.dumps({"unload_models": True, "free_memory": True}).encode()
        self.envoyer(Requete("/free", corps))

    def deposer(self, nom: str, contenu: bytes) -> None:
        """Dépose `contenu` dans ComfyUI/input sous `nom` (écrasé) ; autre nom rendu : échec."""
        frontiere = uuid.uuid4().hex
        corps = _multipart(frontiere, nom, contenu)
        type_ = f"multipart/form-data; boundary={frontiere}"
        reponse = _objet(self.envoyer(Requete("/upload/image", corps, type_)))
        if reponse.get("name") != nom or reponse.get("subfolder", "") != "":
            _echec("image déposée sous un autre nom")


def comfy_depuis_env(env: Mapping[str, str], transport: Transport = _http) -> ClientComfy:
    """Client configuré par COMFY_URL (boucle locale seulement, ex. http://127.0.0.1:8188)."""
    url = env.get("COMFY_URL", "").strip()
    if not url:
        raise ErreurConfiguration("COMFY_URL non configurée")
    return ClientComfy(url, transport=transport)


def decharger_ollama(client: ClientOllama) -> Callable[[], None]:
    """Décharge le modèle d'Ollama de la VRAM (`keep_alive: 0`) ; panne : `OllamaInjoignable`."""

    def decharger() -> None:
        corps = json.dumps({"model": client.modele, "keep_alive": 0}).encode()
        try:
            client.transport(client.url.rstrip("/") + "/api/generate", corps, client.timeout_s)
        except Exception as exc:  # VRAM non libérée : la génération n'est pas lancée
            nom = type(exc).__name__
        else:
            return
        log.warning("ollama dechargement en echec type=%s", nom)
        raise OllamaInjoignable("ollama : déchargement en échec")

    return decharger


@dataclass(frozen=True)
class _Workflow:
    """Workflow API lu tel quel ; marqueurs remplacés dans la structure, jamais dans le texte."""

    graphe: Mapping[str, Any]
    sortie: str  # identifiant du nœud SaveImage / SaveVideo

    @classmethod
    def charger(
        cls, chemin: Path, sauvegarde: str, marqueurs: tuple[str, ...], longueurs: int = 0
    ) -> _Workflow:
        """Workflow valide : sortie `sauvegarde` unique, chaque marqueur une fois, `longueurs`
        entrées `length` entières (nombre d'images remplacé par `frames`)."""
        graphe = _graphe(chemin)
        entrees = [n["inputs"] for n in graphe.values()]
        valeurs = [v for e in entrees for v in e.values()]
        sorties = [k for k, n in graphe.items() if n.get("class_type") == sauvegarde]
        longueur = sum(isinstance(e.get("length"), int) for e in entrees)
        marques = all(valeurs.count(m) == 1 for m in marqueurs)
        if len(sorties) != 1 or not marques or longueur != longueurs:
            raise ErreurConfiguration(f"workflow sans marqueur ou sortie unique : {chemin.name}")
        return cls(graphe, sorties[0])

    def preparer(self, valeurs: Mapping[str, Any], longueur: int | None = None) -> dict[str, Any]:
        graphe: dict[str, Any] = json.loads(json.dumps(self.graphe))
        for noeud in graphe.values():
            entrees = noeud["inputs"]
            for cle, valeur in entrees.items():
                if isinstance(valeur, str) and valeur in valeurs:
                    entrees[cle] = valeurs[valeur]
            if longueur is not None and isinstance(entrees.get("length"), int):
                entrees["length"] = longueur
        return graphe


@dataclass(eq=False)
class EtapesGpu:
    """Étapes `image` et `clip` du pipeline ; appelées sous le verrou GPU (une phase à la fois)."""

    comfy: ClientComfy
    decharger_ollama: Callable[[], None]
    out: Path
    workflows: Path = WORKFLOWS
    delai_image_s: float = DELAI_IMAGE_S
    delai_clip_s: float = DELAI_CLIP_S
    _phase: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self._flux = _Workflow.charger(self.workflows / FLUX, "SaveImage", ("{{PROMPT}}",))
        self._wan = _Workflow.charger(
            self.workflows / WAN, "SaveVideo", ("{{PROMPT}}", "{{IMAGE}}"), longueurs=1
        )

    def image(self, job_id: str, image_prompt: str) -> str:
        """Image Flux déposée dans ComfyUI/input ; rend son chemin relatif `<job_id>.png`."""
        if not _JOB_ID.fullmatch(job_id) or not isinstance(image_prompt, str):
            _echec("job image hors contrat")
        self.decharger_ollama()  # à chaque image : un run voisin a pu recharger Ollama
        self._phase = "image"
        graphe = self._flux.preparer({"{{PROMPT}}": image_prompt})
        png = self.comfy.rendre(graphe, self._flux.sortie, self.delai_image_s, TAILLE_IMAGE)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            _echec("image hors format PNG")
        nom = f"{job_id}.png"
        self.comfy.deposer(nom, png)
        return nom

    def clip(self, job: Mapping[str, Any]) -> str:
        """Clip Wan 2.2 du gpu-job, écrit sous `<out>/<run_id>/<job_id>.mp4` ; rend ce chemin."""
        run_id = _run_id(job)
        if self._phase != "clip":
            self.comfy.liberer()
            self._phase = "clip"
        valeurs = {"{{PROMPT}}": job["motion_prompt"], "{{IMAGE}}": job["image_path"]}
        graphe = self._wan.preparer(valeurs, longueur=job["frames"])
        mp4 = self.comfy.rendre(graphe, self._wan.sortie, self.delai_clip_s, TAILLE_CLIP)
        if mp4[4:8] != b"ftyp":
            _echec("clip hors format MP4")
        return str(_ecrire(self.out / run_id, f"{job['job_id']}.mp4", mp4))


def _run_id(job: Mapping[str, Any]) -> str:
    """run_id tiré du job_id `<run_id>-clip-<n>` d'un gpu-job conforme ; sinon `ErreurComfy`."""
    job_id, image, mouvement = job.get("job_id"), job.get("image_path"), job.get("motion_prompt")
    frames = job.get("frames")
    conforme = (
        job.get("schema_version") == "1.0"
        and job.get("type") == "image_to_clip"
        and isinstance(image, str)
        and bool(_IMAGE_PATH.fullmatch(image))
        and isinstance(mouvement, str)
        and 1 <= len(mouvement) <= 2000
        and type(frames) is int
        and frames in FRAMES
    )
    trouve = _JOB_CLIP.fullmatch(job_id) if isinstance(job_id, str) else None
    if not conforme or trouve is None:
        _echec("gpu-job hors contrat")
    return trouve.group(1)


def _ecrire(dossier: Path, nom: str, contenu: bytes) -> Path:
    """Écrit un nouveau fichier ; dossier en lien symbolique ou fichier existant : `ErreurComfy`."""
    chemin = dossier / nom
    try:
        dossier.mkdir(parents=True, exist_ok=True)
        if dossier.is_symlink():
            raise IsADirectoryError
        fd = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        nom_erreur = type(exc).__name__
    else:
        with os.fdopen(fd, "wb") as fichier:
            fichier.write(contenu)
        return chemin
    log.warning("clip non ecrit type=%s", nom_erreur)
    raise ErreurComfy("clip : écriture refusée")


def _graphe(chemin: Path) -> dict[str, Any]:
    """Graphe API ComfyUI : objet de nœuds ayant chacun un objet `inputs`."""
    try:
        graphe = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        graphe = None
    if isinstance(graphe, dict) and graphe:
        noeuds = graphe.values()
        if all(isinstance(n, dict) and isinstance(n.get("inputs"), dict) for n in noeuds):
            return graphe
    raise ErreurConfiguration(f"workflow illisible : {chemin.name}")


def _fichier(sortie: Any) -> dict[str, str]:
    """Paramètres de `/view` pour la première sortie du nœud ; nom hostile : `ErreurComfy`."""
    listes = [v for v in sortie.values() if isinstance(v, list)] if isinstance(sortie, dict) else []
    elements = [e for liste in listes for e in liste if isinstance(e, dict)]
    element = elements[0] if elements else {}
    nom, sous, type_ = element.get("filename"), element.get("subfolder", ""), element.get("type")
    valide = isinstance(nom, str) and bool(_NOM_SORTIE.fullmatch(nom)) and type_ == "output"
    if not valide or not isinstance(sous, str) or not _SOUS_DOSSIER.fullmatch(sous):
        _echec("fichier de sortie hors contrat")
    return {"filename": str(nom), "subfolder": sous, "type": "output"}


def _multipart(frontiere: str, nom: str, contenu: bytes) -> bytes:
    debut = f"--{frontiere}\r\n"
    parties = [
        f'{debut}Content-Disposition: form-data; name="image"; filename="{nom}"\r\n'
        "Content-Type: image/png\r\n\r\n".encode(),
        contenu,
        f'\r\n{debut}Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode(),
        f'{debut}Content-Disposition: form-data; name="type"\r\n\r\ninput\r\n'.encode(),
        f"--{frontiere}--\r\n".encode(),
    ]
    return b"".join(parties)


def _objet(brut: bytes) -> dict[str, Any]:
    try:
        valeur = json.loads(brut.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        valeur = None
    if not isinstance(valeur, dict):
        _echec("réponse JSON hors contrat")
    return valeur


def _echec(motif: str) -> NoReturn:
    """Lève `ErreurComfy` avec un motif fixe (jamais de prompt ni de texte de ComfyUI)."""
    log.warning("comfyui echec motif=%s", motif)
    raise ErreurComfy(f"comfyui : {motif}")
