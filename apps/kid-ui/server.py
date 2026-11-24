"""Serveur statique minimal de l'appli enfant (stdlib seule, écoute 127.0.0.1 par défaut)."""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090

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

    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, **TYPES}  # noqa: RUF012

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def list_directory(self, path: Any) -> None:
        self.send_error(404)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "path", "").split("?")[0] == "/sw.js":
            self.send_header("Cache-Control", "no-cache")  # le navigateur revérifie le SW
            self.send_header("Service-Worker-Allowed", "/")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Aucun journal d'accès : rien de l'enfant n'est tracé."""


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    p = argparse.ArgumentParser(description="Appli enfant (statique)")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = p.parse_args()
    httpd = make_server(a.host, a.port)
    print(f"kid-ui sur http://{a.host}:{a.port}/")  # noqa: T201
    httpd.serve_forever()


if __name__ == "__main__":
    main()
