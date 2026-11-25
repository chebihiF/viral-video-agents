"""Relais LAN : requête authentifiée (via WireGuard) puis magic packet WoL.

Configuration par variables d'environnement uniquement (rien en dur) :
  WOL_TARGET_MAC   adresse MAC du PC GPU
  WOL_RELAY_TOKEN  jeton Bearer (>= 32 caractères), hors dépôt
  WOL_BIND         adresse d'écoute : l'IP WireGuard du relais (0.0.0.0 refusé)
  WOL_PORT         port TCP (défaut 8089)
  WOL_BROADCAST    broadcast du LAN (défaut 255.255.255.255)
"""

from __future__ import annotations

import hmac
import os
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

_MAC = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$", re.I)


def magic_packet(mac: str) -> bytes:
    if not _MAC.match(mac or ""):
        raise ValueError("adresse MAC invalide")
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    return b"\xff" * 6 + raw * 16


def udp_sender(broadcast: str, port: int = 9):
    def send(packet: bytes) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, (broadcast, port))

    return send


def handle(method, path, auth, mac, token, send):
    """Retourne (code HTTP, message). `send` est injectable."""
    if (
        not auth
        or not auth.startswith("Bearer ")
        or not hmac.compare_digest(auth[7:].encode(), token.encode())
    ):
        return 401, "unauthorized"
    if path != "/wake":
        return 404, "not found"
    if method != "POST":
        return 405, "method not allowed"
    try:
        send(magic_packet(mac))
    except OSError:
        return 502, "send failed"
    return 200, "sent"


def load_config(env) -> dict:
    mac, token, bind = env.get("WOL_TARGET_MAC"), env.get("WOL_RELAY_TOKEN"), env.get("WOL_BIND")
    if not (mac and token and bind):
        sys.exit("WOL_TARGET_MAC, WOL_RELAY_TOKEN et WOL_BIND sont requis")
    try:
        magic_packet(mac)
    except ValueError:
        sys.exit("WOL_TARGET_MAC invalide")
    if len(token) < 32:
        sys.exit("WOL_RELAY_TOKEN trop court (>= 32)")
    if bind in ("0.0.0.0", "::", ""):  # noqa: S104
        sys.exit("WOL_BIND doit être l'IP WireGuard, pas une écoute publique")
    return {
        "mac": mac,
        "token": token,
        "bind": bind,
        "port": int(env.get("WOL_PORT", "8089")),
        "broadcast": env.get("WOL_BROADCAST", "255.255.255.255"),
    }


def main() -> None:  # pragma: no cover
    cfg = load_config(os.environ)
    send = udp_sender(cfg["broadcast"])

    class H(BaseHTTPRequestHandler):
        def _do(self):
            code, msg = handle(
                self.command,
                self.path,
                self.headers.get("Authorization"),
                cfg["mac"],
                cfg["token"],
                send,
            )
            self.send_response(code)
            self.end_headers()
            self.wfile.write(msg.encode())

        do_POST = do_GET = _do

        def log_message(self, *a):  # pas de journal du jeton ni des en-têtes
            pass

    HTTPServer((cfg["bind"], cfg["port"]), H).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
