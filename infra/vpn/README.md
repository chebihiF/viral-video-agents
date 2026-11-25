# infra/vpn : réveil du PC GPU hors du LAN (ADR-005, option A)

Gabarits seulement : rien n'est exécuté ni installé par le dépôt. Aucune clé ici.
Schéma : téléphone/poste -> WireGuard -> VPS (hub 10.8.0.1) <- relais LAN (10.8.0.2) -> magic packet -> PC GPU.

## Comment le parent déploie (hors pipeline pour le LAN, ticket d'infra pour le VPS)
1. Relais LAN (machine toujours allumée, sans veille) : installer WireGuard, copier `wg0.relay.conf.example`
   en `wg0.conf`, générer ses clés localement, remplir les `<...>`.
2. VPS : via un ticket d'infra et le pipeline (pas à la main) ; `wg0.hub.conf.example`.
   Ouvrir seulement UDP 51820 dans le pare-feu Hostinger. n8n et Traefik restent intouchés.
3. Relais : `wol-relay.env.example` -> fichier `600` hors dépôt ; lancer `python3 wol_relay.py`
   (Python 3 standard, aucune dépendance) sous le service de son choix.
4. Test : depuis un pair VPN, `curl -X POST -H "Authorization: Bearer $TOKEN" http://10.8.0.2:8089/wake` ;
   puis `pc-gpu status` doit répondre. Échec : message « une fois à la maison » conservé.

## Retour arrière
Relais : arrêter le service `wol_relay.py`, `wg-quick down wg0`. VPS : `wg-quick down wg0`, retirer le pair,
refermer UDP 51820. L'usage local de `pc-gpu` sur le LAN est inchangé.

## Sécurité
Le relais refuse d'écouter sur 0.0.0.0 (IP WireGuard obligatoire), jeton >= 32 caractères comparé à temps
constant, aucun journal des en-têtes. Un seul endpoint : `POST /wake`.
