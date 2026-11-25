# ADR-005 — Réveil du PC GPU hors du réseau local (VPN)

- Statut : proposé — 2026-11-25 (étape 1, docs seulement ; rien n'est installé ni modifié)
- Liens : #65, #37, #158, ADR-001, `docs/ARCHITECTURE.md` (section réveil à la demande)

## Contexte
Aujourd'hui `pc-gpu wake|sleep|status` ne marche que depuis le réseau local (décision du parent, #158).
Le réveil à distance (Wake-on-LAN) était KO le 18/11 (BIOS) ; le parent l'a corrigé et le test réel du
25/11 passe (réveil en ~9 s, depuis le LAN). Reste le besoin hors maison : l'appli affiche aujourd'hui
« je te génère la vidéo une fois à la maison ».

Contrainte structurante : un magic packet WoL est un broadcast de couche 2. Il ne traverse ni Internet
ni un VPN routé : il faut **une machine toujours allumée sur le LAN** qui l'émet (relais). Le VPN ne
sert qu'à joindre ce relais (ou le Mac relais qui porte déjà `pc-gpu`, cf. #223).

## Options (open source, 0 €)
| Option | Coût | Sécurité | Effort | Dépendance |
|---|---|---|---|---|
| A. WireGuard auto-hébergé sur le VPS (hub), pairs : relais LAN et mobile | 0 € (VPS déjà payé) | Bonne : clés, 1 port UDP, pas de tiers | Moyen : conf serveur + pairs, ouverture d'un port UDP | VPS (déjà SPOF de n8n) ; le relais appelle le VPS en sortie : pas de redirection de port chez soi, passe le CGNAT |
| B. Headscale (serveur de coordination libre, BSD-3) + clients Tailscale | 0 € | Bonne, ACL, mais 1 service de plus à patcher sur un VPS 8 Go partagé avec n8n | Élevé | VPS + maintenance Headscale |
| C. Tailscale, offre gratuite (clients BSD-3 ; serveur de coordination propriétaire, offre perso gratuite, limites susceptibles de changer) | 0 € tant que l'offre dure | Bonne, mais un tiers voit les métadonnées | Faible : aucune action sur le VPS | Service SaaS tiers, conditions modifiables |
| D. Magic packet relayé par un petit relais LAN seul (sans VPN : appel HTTPS authentifié vers le relais) | 0 € si matériel existant | Plus faible : relais exposé ou tunnel à ajouter | Faible | Redirection de port/CGNAT ; ne résout rien seul |

Dans tous les cas, **le relais LAN (D) est nécessaire** : A, B et C ne font que le joindre. Matériel : le Mac
relais s'il reste allumé, sinon un petit appareil toujours allumé (hors budget : à fournir par le parent).

## Dépendance au BIOS
Toutes les options reposent sur le même réglage BIOS/OS (WoL activé, S3/S5, carte réseau). Il est refait
et validé le 25/11 depuis le LAN ; il peut régresser (mise à jour BIOS, pilote). Garde-fou : le test
`pc-gpu wake` / `status` reste le smoke test, et l'échec garde le message « une fois à la maison » (pas de
boucle de réveil, cf. `GpuUnavailable`).

## Recommandation
**Option A + relais LAN** : WireGuard sur le VPS (hub), le relais LAN et le téléphone en pairs. Raisons :
0 €, aucun tiers, licence permissive côté outils, le relais sort en connexion persistante (pas de port
ouvert chez soi). Repli sans toucher au VPS : option C (Tailscale), à n'adopter qu'après accord explicite
du parent sur la dépendance SaaS. Headscale écarté (service en plus pour un seul foyer). Cette ADR ne
déploie rien : l'installation passera par un ticket d'infra, un pipeline et une procédure de retour arrière
(désactiver l'interface WireGuard et fermer le port).

## Gestes que seul le parent peut faire
- Attend l'utilisateur : valider l'option (A recommandée, ou C) et le principe d'un accès hors maison (#65 est reporté).
- Attend l'utilisateur : désigner la machine LAN toujours allumée qui sert de relais (Mac relais ou autre appareil) et confirmer qu'elle ne se met pas en veille.
- Attend l'utilisateur : ouvrir un port UDP dédié dans le pare-feu du VPS Hostinger (option A) ; aucun changement sur n8n ni Traefik.
- Attend l'utilisateur : confirmer le maintien du réglage BIOS WoL après toute mise à jour BIOS, et le ré-tester (`pc-gpu wake`).
- Attend l'utilisateur : côté routeur, réserver l'IP du PC GPU (192.168.1.14) et du relais ; aucune redirection de port n'est requise pour A.
- Attend l'utilisateur : fournir les clés/appareils (téléphone) lors de l'installation ; les clés ne vont jamais dans le dépôt.

## Retour arrière
Étape 1 = docs seulement : supprimer ce fichier. Pour une étape ultérieure : arrêt de l'interface
WireGuard, fermeture du port, retrait des pairs ; l'usage local (`pc-gpu` sur le LAN) reste inchangé.
