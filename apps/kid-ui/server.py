"""Serveur minimal de l'appli enfant (stdlib seule, HTTPS, écoute 127.0.0.1 par défaut).

Statique de static/ + API jobs (#162, contracts/kid-job.schema.json) : POST /api/jobs,
GET /api/jobs/{id}. Hors réseau local (lan_guard, #158) : réponse « deferred », aucun job.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import ssl
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# lan_guard vit dans apps/gpu-worker (#158) : même garde, une seule implémentation.
_GPU_WORKER = str(Path(__file__).resolve().parent.parent / "gpu-worker")
if _GPU_WORKER not in sys.path:
    sys.path.append(_GPU_WORKER)

import jobs  # noqa: E402
import lan_guard  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
TLS_TIMEOUT = 10.0  # secondes : une connexion muette est coupée
LAN = ipaddress.ip_network("192.168.0.0/16")
DEFAULT_RUNS_DIR = Path.home() / ".cache" / "vva" / "kid-ui-runs"
API_JOBS = "/api/jobs"
_JOB_PATH = re.compile(r"/api/jobs/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
BODY_MAX = 8192  # octets : 500 caractères échappés en JSON tiennent largement
DEFERRED = {"status": "deferred", "message": lan_guard.MSG_HORS_RESEAU}

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}


class Handler(SimpleHTTPRequestHandler):
    """GET/HEAD des seuls fichiers de static/ ; pas de listing de dossier, pas de journal."""

    timeout = TLS_TIMEOUT

    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, **TYPES}  # noqa: RUF012

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def setup(self) -> None:
        """Poignée de main TLS dans le fil de la requête, avec délai (jamais dans accept)."""
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(self.timeout)
            self.request.do_handshake()
        super().setup()

    def list_directory(self, path: Any) -> None:
        self.send_error(404)

    def client_ip(self) -> str:
        """Adresse du socket seulement : X-Forwarded-For n'est jamais lu (lan_guard)."""
        return str(self.client_address[0])

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if not path.startswith("/api/"):
            super().do_GET()
        elif not lan_guard.is_local(self.client_ip()):
            self._json(200, DEFERRED)
        elif (m := _JOB_PATH.fullmatch(path)) and (status := self._jobs().status(m[1])):
            self._json(200, dict(status))
        else:
            self._json(404, {"error": "job inconnu"})

    def do_POST(self) -> None:
        if self.path.split("?")[0] != API_JOBS:
            self._json(404, {"error": "introuvable"})
        elif not lan_guard.is_local(self.client_ip()):
            self._json(200, DEFERRED)  # corps non lu : rien de l'enfant ne part
        else:
            self._submit()

    def _submit(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        if size > BODY_MAX:
            self._json(413, {"error": "corps trop gros"})
            return
        try:
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict) or set(body) != {"text"}:
                raise jobs.InvalidText
            job_id = self._jobs().submit(body["text"])
        except (ValueError, UnicodeError):  # JSON invalide ou InvalidText : texte jamais cité
            self._json(400, {"error": "texte refusé"})
            return
        self._json(202, {"job_id": job_id})

    def _jobs(self) -> jobs.JobStore:
        assert isinstance(self.server, Server)
        return self.server.store

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "path", "").split("?")[0] == "/sw.js":
            self.send_header("Cache-Control", "no-cache")  # le navigateur revérifie le SW
            self.send_header("Service-Worker-Allowed", "/")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Aucun journal d'accès : rien de l'enfant n'est tracé."""


class Server(ThreadingHTTPServer):
    store: jobs.JobStore

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Poignée de main avortée ou connexion muette : on ferme sans trace ni journal."""


def make_tls_context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx


def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    store: jobs.JobStore | None = None,
) -> ThreadingHTTPServer:
    """HTTP nu si aucun certificat (réservé aux tests et à --insecure-dev), sinon HTTPS."""
    httpd = Server((host, port), Handler)
    httpd.store = store or jobs.JobStore(DEFAULT_RUNS_DIR)
    if tls_cert and tls_key:
        ctx = make_tls_context(tls_cert, tls_key)
        # Pas de poignée de main dans accept() : un client muet bloquerait tout le serveur.
        httpd.socket = ctx.wrap_socket(
            httpd.socket, server_side=True, do_handshake_on_connect=False
        )
    return httpd


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Appli enfant (statique, HTTPS local)")
    p.add_argument("--host", default=DEFAULT_HOST, help="interface d'écoute (IP LAN du serveur)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--tls-cert", help="certificat PEM (voir infra/kid-ui/README.md)")
    p.add_argument("--tls-key", help="clé privée PEM")
    p.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR, help="dossier des runs vidéo")
    p.add_argument(
        "--insecure-dev",
        action="store_true",
        help="autorise le HTTP sans TLS (développement ; le micro et la PWA n'y fonctionnent pas)",
    )
    return p.parse_args(argv)


def check_host(host: str) -> str | None:
    """Seuls loopback et LAN privé 192.168.0.0/16 : jamais 0.0.0.0, jamais une IP publique."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return f"--host doit être une adresse IP (127.0.0.1 ou 192.168.x.y), reçu : {host}"
    if ip.is_loopback or ip in LAN:
        return None
    return (
        f"--host {host} refusé : seules 127.0.0.1/::1 et le LAN 192.168.0.0/16 sont permis "
        "(jamais 0.0.0.0 ni une adresse publique)."
    )


def validate(a: argparse.Namespace) -> str | None:
    """Message d'erreur si la combinaison d'options est refusée, sinon None."""
    host_err = check_host(a.host)
    if host_err:
        return host_err
    if bool(a.tls_cert) != bool(a.tls_key):
        return "--tls-cert et --tls-key vont ensemble : donnez les deux."
    if a.tls_cert:
        missing = [f for f in (a.tls_cert, a.tls_key) if not Path(f).is_file()]
        if missing:
            return f"fichier TLS introuvable : {missing[0]} (voir infra/kid-ui/make-cert.sh)."
        return None
    if not a.insecure_dev:
        return (
            "refus de démarrer en HTTP : le micro et l'installation PWA exigent HTTPS. "
            "Fournissez --tls-cert et --tls-key (infra/kid-ui/README.md), "
            "ou --insecure-dev pour un essai local sans téléphone."
        )
    return None


def main(argv: list[str] | None = None) -> None:
    a = parse_args(argv)
    err = validate(a)
    if err:
        print(f"kid-ui : {err}", file=sys.stderr)  # noqa: T201
        raise SystemExit(2)
    httpd = make_server(
        a.host, a.port, tls_cert=a.tls_cert, tls_key=a.tls_key, store=jobs.JobStore(a.runs_dir)
    )
    scheme = "https" if a.tls_cert else "http"
    print(f"kid-ui sur {scheme}://{a.host}:{a.port}/")  # noqa: T201
    httpd.serve_forever()


if __name__ == "__main__":
    main()
