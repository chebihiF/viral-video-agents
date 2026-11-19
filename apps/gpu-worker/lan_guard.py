"""Garde réseau local (#158) : le PC GPU n'est utilisé que depuis la maison.

Échec fermé : IP absente, invalide ou hors liste = hors réseau. Seule l'adresse du socket
est fiable ; X-Forwarded-For et consorts ne sont jamais lus.
Bibliothèque standard uniquement, pour tourner tel quel sur le PC GPU.
"""

from __future__ import annotations

import ipaddress

LAN_NETWORKS: tuple[str, ...] = ("192.168.1.0/24",)
MSG_HORS_RESEAU = "Je te génère la vidéo une fois à la maison."


def is_local(client_ip: object, networks: tuple[str, ...] = LAN_NETWORKS) -> bool:
    """Vrai seulement si client_ip est une IP valide appartenant à l'un des réseaux."""
    if not isinstance(client_ip, str):
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return any(addr in ipaddress.ip_network(n, strict=False) for n in networks)
    except ValueError:  # IP ou réseau invalide, ou familles incompatibles
        return False
