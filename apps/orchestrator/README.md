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
- Sans `--mocks` : « étapes réelles non configurées », code 78.
- `--mocks` (`mocks.py`) : étapes factices, sans GPU ni fichier ; le mot-clé `MOCK-BLOQUE` dans la dictée la fait classer « bloque ». `video_path` est fictif.
- La dictée n'est ni écrite, ni affichée, ni journalisée (logs `run=… etape=…` sur stderr).

## Étapes LLM réelles (#137)

```python
from orchestrator.steps_llm import classifier_ollama, client_depuis_env, storyboard_ollama

client = client_depuis_env()  # OLLAMA_URL = racine du serveur, ex. http://hote:11434
etapes = Etapes(classifier_ollama(client), storyboard_ollama(client), voix, image, clip, montage)
```

- `POST <OLLAMA_URL>/api/chat`, modèle `qwen3:8b`, `think: false`, `temperature: 0`, `seed: 42`, `format` = schéma du contrat (sans les règles conditionnelles, vérifiées après).
- Prompts : bloc `text` de `prompts/classification.v1.md` (dictée encadrée par `<dictee>…</dictee>`, balises internes neutralisées) ; corps de `prompts/storyboard.v1.md` (texte autorisé tel quel).
- Sortie validée contre `contracts/classification.schema.json` / `storyboard.schema.json` par un validateur stdlib aligné sur `jsonschema` (testé) ; mot-clé de schéma non géré = `ErreurConfiguration`.
- Échec fermé : sortie illisible, hors schéma, NaN ou réponse > 1 Mo → un seul nouvel essai, puis `SortieNonConforme` ; serveur injoignable → `OllamaInjoignable` ; `OLLAMA_URL` vide ou non http(s) → `ErreurConfiguration`. Jamais de classification « libre » par défaut : le run passe en « erreur ».
- Ni dictée ni sortie du modèle dans les logs ou les exceptions (sans `__cause__` ni `__context__`).
- Transport injectable `(url, corps, timeout) -> octets` : faux Ollama en test, aucun appel réseau.
