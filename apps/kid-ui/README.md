# kid-ui

Appli enfant « Mimo » (#159) : coquille statique installable sur l'écran d'accueil du téléphone (PWA). Aucune dépendance, aucun appel réseau.

## Lancer

```sh
python3 apps/kid-ui/server.py --tls-cert infra/kid-ui/certs/cert.pem --tls-key infra/kid-ui/certs/key.pem [--host 192.168.1.20] [--port 8090]
python3 apps/kid-ui/server.py --insecure-dev   # HTTP local, développement seulement
```

Pour l'API jobs (#162), lancer avec l'environnement du dépôt (`uv run python apps/kid-ui/server.py …`) : le serveur appelle `python -m orchestrator run --dictee-file - --out <runs>/<job_id>` (sans `--mocks`, configuration `OLLAMA_URL`, `PIPER_BIN`, `PIPER_MODEL` requise, sinon job `error`). Runs dans `--runs-dir` (défaut `~/.cache/vva/kid-ui-runs`).

Écoute `127.0.0.1` par défaut ; `--host` pour l'IP LAN. Sans certificat, le serveur refuse de démarrer (HTTP) sauf `--insecure-dev`. Génération du certificat et installation sur le téléphone : `infra/kid-ui/README.md` (#160).

## Contenu de `static/`

| Fichier | Rôle |
|---|---|
| `index.html` | coquille : écran d'accueil + écran « Oups, pas de connexion » (affiché hors ligne) |
| `manifest.webmanifest` | `standalone`, `start_url /`, `lang fr`, couleurs du thème nuit `#120E2B`, icônes 192/512/maskable |
| `sw.js` | précache de la coquille uniquement (liste `PRECACHE`) ; jamais de `/api`, audio, vidéo ni texte dicté |
| `theme.js` | pose `data-theme` sur `<html>` depuis `localStorage["kid-theme"]` avant le rendu ; rien n'est envoyé au serveur |
| `status.js` | envoi de la dictée (`MimoStatus.envoyer(texte)`) et suivi toutes les 2 s : écrans Envoi, Vidéo en cours, Hors maison, Erreur |
| `shell.js` | enregistre le service worker, bascule l'écran hors ligne, choix du thème |
| `tokens-enfant.css`, `shell.css`, `mimo.svg` | tokens de la maquette (#36) et mascotte |
| `icons/` | générées localement : `python3 apps/kid-ui/make_icons.py` (stdlib, rien de téléchargé) |

## API jobs (#162, `contracts/kid-job.schema.json`)

- `POST /api/jobs` `{"text": "…"}` (≤ 500 caractères) → 202 `{"job_id": "<uuid>"}` ; texte invalide → 400.
- `GET /api/jobs/{id}` → `{"state": "queued|running|done|error", "step": "storyboard|voix|images|montage"}` ; inconnu → 404.
- Client hors `192.168.1.0/24` (`apps/gpu-worker/lan_guard.py`) : 200 `{"status": "deferred", "message": "Je te génère la vidéo une fois à la maison."}`, aucun job. En `--insecure-dev` sur `127.0.0.1`, le client est donc « hors maison ».
- La dictée passe par stdin du CLI, jamais en argument, variable d'environnement ni journal ; seul `job_id` est journalisé. Un parcours à la fois.

## Polices

Pas de téléchargement : la maquette prévoit Bricolage Grotesque et Figtree (OFL), mais elles ne sont pas encore hébergées. Les piles de `tokens-enfant.css` retombent sur les polices système (Arial Rounded / Trebuchet / system-ui). À héberger en local dans un ticket dédié.

## Tests

`uv run pytest apps/kid-ui` : manifest, service worker (liste de précache), absence de contenu sensible en cache, serveur local. `apps/kid-ui/tests/test_tls.py` : démarrage HTTPS (certificat jetable) et refus du HTTP nu. L'installation réelle sur téléphone se vérifie à la main (`infra/kid-ui/README.md`).
