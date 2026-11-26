# kid-ui

Appli enfant « Mimo » (#159) : coquille statique installable sur l'écran d'accueil du téléphone (PWA). Aucune dépendance, aucun appel réseau.

## Lancer

```sh
python3 apps/kid-ui/server.py --tls-cert infra/kid-ui/certs/cert.pem --tls-key infra/kid-ui/certs/key.pem [--host 192.168.1.20] [--port 8090]
python3 apps/kid-ui/server.py --insecure-dev   # HTTP local, développement seulement
python3 apps/kid-ui/server.py --mock-asr ...   # démo de la dictée : texte fixe, aucune vraie transcription
uv run --extra asr python apps/kid-ui/server.py --asr faster-whisper [--asr-model <dossier>] ...   # ASR local réel (#217)
```

Dictée (#161) : `POST /api/dictation` répond 503 sans `--asr` ni `--mock-asr` ; `--mock-asr` sert uniquement de démo (texte fixe, jamais en production). `--asr` et `--mock-asr` sont exclusifs.

### ASR local darija (#217) : faster-whisper sur CPU

`--asr faster-whisper` charge au démarrage un modèle CTranslate2 en int8 sur le CPU du Mac (`device=cpu`, `local_files_only=True` : aucun téléchargement ni appel réseau au lancement ni à la dictée). L'audio est décodé en mémoire (`BytesIO`) : jamais écrit sur disque, jamais journalisé. Le modèle est cherché dans `--asr-model`, sinon `$VVA_ASR_MODEL`, sinon `~/.cache/vva/asr/whisper-small-darija-ct2`. Si le dossier ou `faster-whisper` manque, le serveur refuse de démarrer (code 2) au lieu de répondre 500 à chaque dictée.

Choix (scout, #217) : `faster-whisper` (MIT) + `Anass-Srk/fine-tuned-whisper-small-darija` (Apache-2.0). Le darija se transcrit avec `language="ar"`. **Le WER et la latence (estimation ~3-6 s pour 30 s, non mesurée) restent à mesurer sur de l'audio darija réel** avant de s'y fier ; rien n'est chiffré ici.

Installation et conversion du modèle (une fois, à la main, avec réseau ; jamais exécutées en CI, qui n'installe ni `faster-whisper` ni modèle et teste avec un faux module) :

```sh
uv sync --extra asr                       # installe faster-whisper (extra optionnel)
uv run --with ctranslate2 --with transformers --with torch ct2-transformers-converter \
  --model Anass-Srk/fine-tuned-whisper-small-darija \
  --output_dir ~/.cache/vva/asr/whisper-small-darija-ct2 \
  --copy_files tokenizer.json preprocessor_config.json --quantization int8
```

Vérifier la licence affichée sur la page du modèle avant usage, puis lancer avec `--asr faster-whisper`. Les fichiers du modèle restent hors dépôt.
Pour l'API jobs (#162), lancer avec l'environnement du dépôt (`uv run python apps/kid-ui/server.py …`) : le serveur appelle `python -m orchestrator run --dictee-file - --out <runs>/<job_id>` (sans `--mocks`, configuration `OLLAMA_URL`, `SILMA_BIN`, `SILMA_MODEL_DIR`, `SILMA_VOIX`, `VOIX_OFF_ID` et `COMFY_URL` requise, sinon job `error` ; voir `apps/orchestrator/README.md`). Runs dans `--runs-dir` (défaut `~/.cache/vva/kid-ui-runs`).

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
- `GET /api/jobs/{id}` → `{"state": "queued|running|done|error", "step": "storyboard|voix|images|montage"}` (+ `blocked`) ; inconnu → 404.
- Client hors `192.168.1.0/24` (`apps/gpu-worker/lan_guard.py`) : 200 `{"status": "deferred", "message": "Je te génère la vidéo une fois à la maison."}`, aucun job.
- **Loopback = hors maison** : un client `127.0.0.1` ou `::1` reçoit `deferred` (lan_guard n'accepte que le LAN). En `--insecure-dev` sur `127.0.0.1`, aucun job ne part ; tester depuis le téléphone, ou `--host 192.168.1.x`.
- Anti-CSRF : `Content-Type: application/json` exigé, et `Origin` (si présente) = hôte servi (`Host`), sinon 403. `Content-Length` absent, négatif ou non entier : 400 ; > 8 Kio : 413.
- Un seul job actif : nouvelle soumission pendant un job `queued`/`running` → 409. 50 jobs au plus en mémoire (les plus anciens terminés sont oubliés). Délai global 30 min, puis kill et `error`.
- Dictée bloquée par la classification (code 2 du CLI) : `{"state": "error", "blocked": true}` ; `status.js` désactive « Réessayer » et propose « Autre idée ».
- Avancement (#165) : `GET /api/jobs/{id}` ajoute `progress {pct, restant_s, bientot}` (absent en `error`, 100 % à `done`). `eta.py` estime d'après la médiane par étape des 10 derniers runs réels terminés lus dans `<runs-dir>/*/durees.jsonl` (lecture seule) ; sans mesure, constantes `DEFAUT_S` (ordre de grandeur a priori, non mesuré). `bientot` sous 30 s ou en dépassement ; `restant_s` null si inconnu. Le pourcentage ne recule jamais ; `status.js` affiche la barre et « Encore environ N min » (jamais de secondes, jamais 0).
- La dictée passe par stdin du CLI, jamais en argument, variable d'environnement ni journal ; seul `job_id` est journalisé.

## Polices

Pas de téléchargement : la maquette prévoit Bricolage Grotesque et Figtree (OFL), mais elles ne sont pas encore hébergées. Les piles de `tokens-enfant.css` retombent sur les polices système (Arial Rounded / Trebuchet / system-ui). À héberger en local dans un ticket dédié.

## Tests

`uv run pytest apps/kid-ui` : manifest, service worker (liste de précache), absence de contenu sensible en cache, serveur local. `apps/kid-ui/tests/test_tls.py` : démarrage HTTPS (certificat jetable) et refus du HTTP nu. L'installation réelle sur téléphone se vérifie à la main (`infra/kid-ui/README.md`).
