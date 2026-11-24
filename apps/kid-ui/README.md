# kid-ui

Appli enfant « Mimo » (#159) : coquille statique installable sur l'écran d'accueil du téléphone (PWA). Aucune dépendance, aucun appel réseau.

## Lancer

```sh
python3 apps/kid-ui/server.py [--host 127.0.0.1] [--port 8090]
```

Écoute `127.0.0.1` par défaut (l'exposition HTTPS, nécessaire à l'installation sur téléphone, relève de #160).

## Contenu de `static/`

| Fichier | Rôle |
|---|---|
| `index.html` | coquille : écran d'accueil + écran « Oups, pas de connexion » (affiché hors ligne) |
| `manifest.webmanifest` | `standalone`, `start_url /`, `lang fr`, couleurs du thème nuit `#120E2B`, icônes 192/512/maskable |
| `sw.js` | précache de la coquille uniquement (liste `PRECACHE`) ; jamais de `/api`, audio, vidéo ni texte dicté |
| `theme.js` | pose `data-theme` sur `<html>` depuis `localStorage["kid-theme"]` avant le rendu ; rien n'est envoyé au serveur |
| `shell.js` | enregistre le service worker, bascule l'écran hors ligne, choix du thème |
| `tokens-enfant.css`, `shell.css`, `mimo.svg` | tokens de la maquette (#36) et mascotte |
| `icons/` | générées localement : `python3 apps/kid-ui/make_icons.py` (stdlib, rien de téléchargé) |

## Polices

Pas de téléchargement : la maquette prévoit Bricolage Grotesque et Figtree (OFL), mais elles ne sont pas encore hébergées. Les piles de `tokens-enfant.css` retombent sur les polices système (Arial Rounded / Trebuchet / system-ui). À héberger en local dans un ticket dédié.

## Tests

`uv run pytest apps/kid-ui` : manifest, service worker (liste de précache), absence de contenu sensible en cache, serveur local. L'installation réelle (HTTPS, téléphone) se vérifie à la main une fois #160 livré.
