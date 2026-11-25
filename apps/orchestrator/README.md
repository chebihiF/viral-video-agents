# orchestrator

Orchestrateur Python du pipeline vidéo (#53) : classification, storyboard, voix, image, clip, montage pour une dictée. Étapes injectables (factices en test, réelles ensuite via n8n / gpu-worker / media-service), stdlib seule, importable en Python 3.9+.

## Usage

```python
from orchestrator.pipeline import Etapes, lancer

etapes = Etapes(classifier, storyboard, voix, image, clip, montage)
run = lancer(dictee, etapes, workdir="/data/jobs")  # run_id optionnel (sinon aléatoire)
run["etat"], run.get("video_path")  # ok | bloque | erreur
```

| Étape | Signature | Contrat |
|---|---|---|
| `classifier` | `(dictee) -> classification` | `classification.schema.json` |
| `storyboard` | `(texte) -> storyboard` | `storyboard.schema.json` |
| `voix` | `(tts_job) -> TtsResult` | `tts-job.schema.json` |
| `image` (GPU) | `(job_id, image_prompt) -> image_path` | `image_path` du `gpu-job` |
| `clip` (GPU) | `(gpu_job) -> clip_path` | `gpu-job.schema.json` |
| `montage` | `(montage_job) -> video_path` | `montage-job.schema.json` |

- Sortie : [`contracts/pipeline-run.schema.json`](../../contracts/pipeline-run.schema.json) (exemple `contracts/examples/pipeline-run.ok.json`).
- `libre` : le storyboard reçoit la dictée ; `adouci` : le `texte_adouci`, jamais la dictée ; `bloque` : aucune étape de génération, run « bloque ».
- Second filtre (#244) : le champ `classification` du storyboard est relu. `bloque` : run « bloque » (étapes `classification` et `storyboard` en ok, ni voix, ni image, ni clip, ni montage, CLI code 2) ; absent ou hors `libre|adouci|bloque` : run « erreur » à l'étape `storyboard` (échec fermé).
- Échec d'une étape (exception ou sortie hors contrat) : run « erreur », dernière étape en `statut: erreur`, suivantes non appelées ; seul le type de l'exception est journalisé.
- GPU en série : verrou commun à tous les runs du processus (`GPU_LOCK`, injectable via `gpu_lock`), pris une fois pour toute la phase `image` (tous les plans), puis une fois pour la phase `clip` : un seul changement de modèle ComfyUI, plans d'un run non entrelacés avec un autre run.
- `job_id` GPU distincts par étape (clé d'idempotence du gpu-job) : `<run_id>-img-<n>`, `<run_id>-clip-<n>`.
- Sorties vérifiées : `image` doit rendre un `image_path` conforme au gpu-job (relatif, sans `..`) ; `clip` et les WAV un chemin non vide sans NUL ni segment `..` ; WAV associés aux plans par `plan_id` (manquant ou doublon : erreur de l'étape `voix`).
- Clips de 49 images (2 s à 24 i/s) ; le montage les boucle sur la durée de la voix.
- Aucun texte de dictée, texte adouci ni message d'exception dans l'état ou les logs (`run=… etape=… statut=… duree_s=…`).
- Les dossiers (`<workdir>/<run_id>/voix`, `final.mp4`) sont créés par les étapes, pas par l'orchestrateur.

## CLI (#138)

```
python -m orchestrator run --mocks --dictee-file F --out DIR   # F = fichier UTF-8 ou « - » (stdin)
make run-mock [DICTEE=F]                                        # sortie dans .venv/run-mock
```

- Écrit `DIR/run.json` (`pipeline-run.schema.json`, sans dictée) ; `etat` ok : code 0, erreur : 1, bloque : 2.
- Dictée vide, illisible ou de plus de 2000 caractères, ou arguments invalides : code 64, rien d'écrit.
- Sans `--mocks` : étapes réelles (Ollama, Piper, ComfyUI, montage) ; `OLLAMA_URL`, `PIPER_BIN`, `PIPER_MODEL` ou `COMFY_URL` absente ou invalide : code 78, aucun appel réseau. Clips téléchargés sous `DIR/<run_id>/`.
- `--mocks` (`mocks.py`) : étapes factices, sans GPU ni fichier ; le mot-clé `MOCK-BLOQUE` dans la dictée la fait classer « bloque ». `video_path` est fictif.
- La dictée n'est ni écrite, ni affichée, ni journalisée (logs `run=… etape=…` sur stderr).

## Étapes LLM réelles (#137)

```python
from orchestrator.steps_llm import classifier_ollama, client_depuis_env, storyboard_ollama

client = client_depuis_env()  # OLLAMA_URL = racine du serveur, ex. http://hote:11434
etapes = Etapes(classifier_ollama(client), storyboard_ollama(client), voix, image, clip, montage)
```

- `POST <OLLAMA_URL>/api/chat`, modèle `qwen3:8b`, `think: false`, `temperature: 0`, `seed: 42`, `format` = schéma du contrat (sans les règles conditionnelles, vérifiées après).
- Prompts : bloc `text` de `prompts/classification.v1.md` (dictée normalisée NFKC, encadrée par `<dictee>…</dictee>`, tout `<` `>` de la dictée remplacé par `‹` `›`) ; corps de `prompts/storyboard.v1.md` (texte autorisé tel quel).
- Sortie validée contre `contracts/classification.schema.json` / `storyboard.schema.json` par un validateur stdlib aligné sur `jsonschema` (testé) ; mot-clé ou `type` de schéma non géré = `ErreurConfiguration`.
- Échec fermé : sortie illisible, hors schéma, NaN, JSON trop imbriqué ou réponse > 1 Mo → `SortieNonConforme` ; serveur injoignable ou redirection (jamais suivie) → `OllamaInjoignable` ; proxy d'environnement (`HTTP(S)_PROXY`) ignoré ; `OLLAMA_URL` vide ou non http(s) → `ErreurConfiguration`. Jamais de classification « libre » par défaut : le run passe en « erreur ».
- Pas de nouvel essai (décision #143) : à `temperature: 0` et seed fixe, la même requête rend la même réponse ; un retry doublerait l'attente (2 × 300 s) sans chance de succès. Échec fermé, 300 s au plus.
- Ni dictée ni sortie du modèle dans les logs ou les exceptions (sans `__cause__` ni `__context__`).
- Transport injectable `(url, corps, timeout) -> octets` : faux Ollama en test, aucun appel réseau.

## Étapes média réelles (#136)

```python
from orchestrator.steps_media import montage_reel, voix_piper

# PIPER_BIN / PIPER_MODEL lus dans os.environ
etapes = Etapes(classifier, storyboard, voix_piper(), image, clip, montage_reel())
```

- Installation : extra `media` (`orchestrator[media]`, dépendance locale media-service) ; Python 3.12 requis pour ce module seul, le cœur reste stdlib / 3.9+.
- `voix_piper(env=None)` : `tts-job` -> `TtsResult` via `media_service.tts.synthetiser` ; crée `output_dir` ; `PIPER_BIN` / `PIPER_MODEL` lus dans `env` (sinon l'environnement). Piper ou modèle absent : `ErreurConfiguration` claire avant toute synthèse, le pipeline range le run en « erreur » sur l'étape `voix`.
- `montage_reel()` : durées des WAV (ffprobe) -> `subtitles.write_ass` (`<sortie>.ass` à côté du MP4) -> `run_montage` avec `subtitles_path` -> chemin du MP4 (1080x1920). L'ASS est supprimé si le montage échoue.
- Pas de fuite : erreurs du montage réécrites en `ErreurMontage` (type seul, sans le message de FFmpeg), ni cause ni contexte ; les messages de voix nomment le plan, jamais son texte.

## Étapes GPU réelles via ComfyUI (#245)

```python
from orchestrator.steps_gpu import EtapesGpu, comfy_depuis_env, decharger_ollama

gpu = EtapesGpu(comfy_depuis_env(os.environ), decharger_ollama(client_ollama), out=Path("runs"))
etapes = Etapes(classifier, storyboard, voix, gpu.image, gpu.clip, montage)
```

- `COMFY_URL` : boucle locale seulement (`http://127.0.0.1:8188`, `http://localhost:8188`, `http://[::1]:8188`), par exemple l'extrémité d'un tunnel ouvert à la main. Absente, autre hôte, identifiants, chemin ou requête dans l'URL : `ErreurConfiguration` (CLI : code 78), aucun appel réseau.
- `image(job_id, prompt)` : `comfyui/workflows/flux_schnell_t2i_api.json` (`{{PROMPT}}` remplacé dans la structure JSON) → `/prompt`, `/history/{id}`, `/view` (PNG vérifié) → `/upload/image` (`overwrite`) ; rend `<job_id>.png`, relatif à `ComfyUI/input`. Nom rendu différent : échec.
- `clip(gpu_job)` : gpu-job vérifié (`job_id` `<run_id>-clip-<n>`, `image_path`, `frames` 4n+1) → `wan22_5b_i2v_api.json` (`{{PROMPT}}` = `motion_prompt`, `{{IMAGE}}` = `image_path`, `length` = `frames`) ; MP4 (`ftyp` vérifié) écrit sous `<out>/<run_id>/<job_id>.mp4` (création exclusive, sans suivre de lien symbolique).
- VRAM 8 Go : Ollama déchargé (`POST <OLLAMA_URL>/api/generate`, `keep_alive: 0`) avant chaque image (un run voisin a pu le recharger) ; `POST /free` (`unload_models`, `free_memory`) une fois en entrant dans la phase clip. Appels faits sous le verrou GPU du pipeline.
- Durcissement (comme `steps_llm`) : proxy d'environnement ignoré, redirection refusée, 30 s par requête, rendu borné (image 600 s, clip 1800 s, `/history` lu chaque seconde), réponses bornées (JSON 1 Mo, PNG 50 Mo, MP4 200 Mo). Les noms de fichiers rendus par ComfyUI ne servent qu'à `/view` (validés : ni `..`, ni `/`) ; jamais de chemin local.
- Échec (`ErreurComfy`, `OllamaInjoignable`) : message générique, type seul journalisé, ni prompt ni texte de ComfyUI ; le run est « erreur » sur l'étape, sans MP4 final.
- Tests : `tests/test_steps_gpu.py` (`FauxComfy` en mémoire et derrière un serveur HTTP sur 127.0.0.1) ; aucun GPU, aucun PC distant.

## Parcours de bout en bout avec mocks (#139)

```
make e2e-mock   # pytest -m e2e : dictée -> MP4, ffmpeg local requis, sans GPU ni réseau
```

- `tests/test_e2e_mock.py` pilote le CLI (`orchestrator.__main__.main`) avec des étapes injectées (`tests/fakes.py`) : classification et storyboard factices, **faux Piper** (script écrit en dossier temporaire, 0,25 s par mot), image et clip factices (clips `lavfi`, GPU tracé), montage FFmpeg **réel** avec ASS incrusté.
- Scénarios : MP4 H.264 + AAC 1080x1920 de durée = somme des WAV (±0,2 s) ; un `.ass` à côté du MP4 avec un événement par mot ; dictée « bloque » : aucun MP4, run « bloque » ; Piper absent : run « erreur » sur `voix`, aucun MP4 ni ASS ; étapes image/clip de deux runs concurrents sans chevauchement.
- Marqueur `e2e` ; test sauté si `ffmpeg` / `ffprobe` sont absents. Sorties en dossier temporaire pytest, dictée de test fixe et neutre, aucune dictée dans les logs ni `run.json`.
- Aussi exécuté par `make test` / `make check`.

## Purge des runs anciens (#152)

```
python -m orchestrator purge --runs DIR --older-than-days N            # liste seulement (défaut)
python -m orchestrator purge --runs DIR --older-than-days N --apply    # supprime
```

- Ne considère que les sous-dossiers directs de `DIR` contenant un `run.json` valide (`schema_version` 1.0, `run_id`, `etat`) ; âge = date de modification de `run.json` ; supprimé si plus de N jours.
- Ignorés, jamais touchés : dossier sans `run.json` valide, lien symbolique (dossier de run ou `run.json`) ; les liens internes à un run sont retirés sans être suivis, rien hors de `DIR` n'est affecté.
- `--dry-run` est accepté (comportement par défaut) ; `--apply` avec `--dry-run` est refusé.
- Sortie : une ligne par run ancien, `<run_id> age_jours=<n> a_supprimer|supprime` ; jamais de texte de dictée.
- Codes : 0 ok, 1 suppression échouée (type d'exception seul), 2 usage (N < 1, N non entier, `DIR` absent ou lien symbolique, arguments invalides).
