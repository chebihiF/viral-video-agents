"""Tests de l'appli enfant installable (#159) : manifest, service worker, coquille, serveur."""

from __future__ import annotations

import json
import re
import shutil
import socket
import struct
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import server

STATIC = Path(server.STATIC_DIR)
SENSITIVE_EXT = (".wav", ".mp3", ".ogg", ".m4a", ".mp4", ".webm", ".mov", ".txt")


@pytest.fixture(scope="module")
def base() -> Iterator[str]:
    httpd = server.make_server("127.0.0.1", 0)
    assert isinstance(httpd, ThreadingHTTPServer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - URL locale de test
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), b""


def precache_list() -> list[str]:
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    m = re.search(r"const PRECACHE = (\[.*?\]);", src, re.S)
    assert m, "liste PRECACHE introuvable dans sw.js"
    return list(json.loads(m[1]))


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])  # type: ignore[no-any-return]


# --- Scénario : manifest valide ---
def test_manifest_valide(base: str) -> None:
    status, ctype, body = get(base + "/manifest.webmanifest")
    assert status == 200
    assert ctype.startswith("application/manifest+json")
    m = json.loads(body)
    assert m["display"] == "standalone"
    assert m["start_url"] == "/"
    assert m["name"] and m["lang"] == "fr"
    assert m["theme_color"] == m["background_color"] == "#120E2B"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any("maskable" in i.get("purpose", "") for i in m["icons"])


def test_icones_png_aux_bonnes_tailles(base: str) -> None:
    m = json.loads(get(base + "/manifest.webmanifest")[2])
    for icon in m["icons"]:
        status, ctype, _ = get(base + icon["src"])
        assert (status, ctype) == (200, "image/png")
        w, h = icon["sizes"].split("x")
        assert png_size(STATIC / icon["src"].lstrip("/")) == (int(w), int(h))


# --- Scénario : service worker enregistré ---
def test_sw_servi_en_javascript_et_precache_la_coquille(base: str) -> None:
    status, ctype, _ = get(base + "/sw.js")
    assert status == 200
    assert "javascript" in ctype
    liste = precache_list()
    assert {"/", "/index.html", "/tokens-enfant.css", "/mimo.svg"} <= set(liste)
    assert any(p.startswith("/icons/") and p.endswith(".png") for p in liste)
    for chemin in liste:  # tout ce qui est précaché existe et est servi
        assert get(base + chemin)[0] == 200, chemin


def test_index_lie_manifest_theme_et_enregistre_le_sw(base: str) -> None:
    _, ctype, body = get(base + "/")
    html = body.decode()
    assert ctype.startswith("text/html")
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'name="theme-color" content="#120E2B"' in html
    head = html.split("</head>")[0]
    assert '<script src="/theme.js"></script>' in head  # synchrone : avant le rendu
    assert "serviceWorker.register('/sw.js'" in (STATIC / "shell.js").read_text(encoding="utf-8")
    assert 'id="ecran-erreur"' in html and 'id="ecran-accueil"' in html


# --- Scénario : rien de sensible en cache ---
def test_rien_de_sensible_dans_le_precache() -> None:
    for chemin in precache_list():
        bas = chemin.lower()
        assert chemin.startswith("/") and "?" not in chemin
        assert not bas.startswith("/api"), chemin
        assert not bas.endswith(SENSITIVE_EXT), chemin
        assert "dict" not in bas, chemin


def test_precache_contient_les_polices_arabes_existantes() -> None:
    liste = precache_list()
    for graisse in (500, 700, 800):
        chemin = f"/fonts/tajawal-arabic-{graisse}.woff2"
        assert chemin in liste, chemin
        assert (STATIC / chemin.lstrip("/")).is_file(), chemin


def test_sw_ne_met_rien_en_cache_dynamiquement() -> None:
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "/api" not in src
    assert ".put(" not in src and ".add(" not in src  # seul addAll() à l'installation
    assert src.count("addAll(") == 1


# --- Thème et confidentialité ---
def test_theme_local_sans_reseau() -> None:
    src = (STATIC / "theme.js").read_text(encoding="utf-8")
    assert "localStorage" in src and "kid-theme" in src
    assert "try" in src and "catch" in src
    for interdit in ("fetch", "XMLHttpRequest", "sendBeacon", "WebSocket"):
        assert interdit not in src
    assert "fetch(" not in (STATIC / "shell.js").read_text(encoding="utf-8")


def test_aucune_ressource_externe() -> None:
    for f in STATIC.rglob("*"):
        if f.suffix in {".html", ".css", ".js", ".svg", ".webmanifest"}:
            assert not re.search(r"https?://(?!www\.w3\.org)", f.read_text("utf-8")), f.name


# --- Serveur ---
def test_serveur_ecoute_en_local_par_defaut() -> None:
    assert server.DEFAULT_HOST == "127.0.0.1"


def test_serveur_refuse_traversee_et_listing(base: str) -> None:
    assert get(base + "/../server.py")[0] == 404
    assert get(base + "/%2e%2e/server.py")[0] == 404
    assert get(base + "/icons/")[0] == 404
    assert get(base + "/inconnu")[0] == 404


def test_serveur_survit_a_une_requete_mal_formee(base: str) -> None:
    port = int(base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"CECI N'EST PAS HTTP\r\n\r\n")
        assert b"400" in s.recv(512)  # erreur 400 propre (sans ligne de statut en HTTP/0.9)
    assert get(base + "/manifest.webmanifest")[0] == 200


# --- Service worker : navigations et version du cache ---
def test_sw_navigations_reseau_d_abord() -> None:
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    m = re.search(r'req\.mode === "navigate"\) \{\s*event\.respondWith\((.*?)\);\s*\}', src, re.S)
    assert m, "branche navigation introuvable"
    assert m[1].startswith("fetch(req).catch(")  # réseau puis repli sur la coquille
    assert 'caches.match("/index.html")' in m[1]


def _noms_de_cache(precache: list[str]) -> str:
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    liste = src[src.index("const PRECACHE") :]
    script = (
        "const self = {}; const caches = {}; "
        + liste.split("self.addEventListener")[0]
        + "console.log(CACHE)"
    ).replace(
        re.search(r"const PRECACHE = \[.*?\];", liste, re.S)[0],
        f"const PRECACHE = {json.dumps(precache)};",
    )  # type: ignore[index]
    out = subprocess.run(  # noqa: S603
        ["node", "-e", script],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return out.stdout.strip()


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_nom_du_cache_derive_de_precache() -> None:
    liste = precache_list()
    a = _noms_de_cache(liste)
    assert a.startswith("mimo-shell-")
    assert a == _noms_de_cache(liste)  # déterministe
    assert a != _noms_de_cache([*liste, "/nouveau.css"])  # la liste change => nouveau cache


def test_nom_du_cache_non_fige_dans_le_code() -> None:
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert re.search(r'const CACHE = "mimo-shell-" \+ hashList\(PRECACHE\);', src)
