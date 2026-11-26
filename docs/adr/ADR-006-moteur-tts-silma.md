# ADR-006 : moteur de synthèse vocale SILMA TTS, une voix par réplique

- Statut : accepté (26/11, #249). Le moteur réel n'est **pas encore validé** : il n'est pas installé et aucun essai n'a été fait.
- Contexte : #239 (choix du parent « SILMA TTS »), épique #251, suites #250 (storyboard à répliques), #253 (montage), #254 (une voix par personnage).

## Contexte

Les personnages parlent, chacun avec sa voix ; la voix off est occasionnelle. Les personnages parleront en **darija marocaine** (décision du parent, 26/11). Piper, la solution de test, n'a pas de voix arabe sur le PC et ne clone pas de voix. Le parent a choisi d'**essayer SILMA TTS en darija** : la fiche du modèle annonce l'arabe standard (fusha) et l'anglais, **pas la darija**. La qualité en darija est **non testée**, à juger à l'oreille au ticket d'installation. Rien dans ce dépôt ne l'affirme.

## Décision

1. **SILMA TTS v1** remplace Piper. Le moteur reçoit un texte et une voix de référence, il rend un WAV. Une réplique de personnage et une voix off passent par la même interface.
2. **Contrat `tts-job` 2.0** (incompatible) : `repliques[]` (`replique_id`, `voix_id` logique, `texte`, `langue`), `TtsResult` 2.0 avec `repliques[]`. Le contrat ne contient aucun chemin de voix de référence.
3. **Configuration hors dépôt** :
   - `SILMA_BIN` : un lanceur hors dépôt, `exec <venv SILMA>/bin/python <dépôt>/apps/media-service/scripts/silma_cli.py "$@"`. Le shebang de l'enveloppe prendrait le `python3` du PATH, pas celui du venv.
   - `SILMA_MODEL_DIR` : les poids locaux, annexes compris.
   - `SILMA_VOIX` : un fichier JSON qui associe à chaque `voix_id` son audio de référence et sa transcription.

   Aucune voix par défaut, aucun repli (ni sur une autre voix, ni sur Piper). `PIPER_BIN` et `PIPER_MODEL` disparaissent. Transition jusqu'à #250 : une réplique par plan, toutes avec la voix `VOIX_OFF_ID`.
4. **Isolation** : le media-service reste en bibliothèque standard. `silma_tts`, torch et leurs dépendances ne sont dans aucun `pyproject.toml` ni dans `uv.lock`, et la CI ne les installe pas. Le moteur tourne dans un processus séparé, et son environnement Python est séparé lui aussi.
5. **Données de l'enfant** : le texte et la transcription passent par stdin (JSON), jamais dans les arguments, les logs, les exceptions ni un fichier temporaire. Les sorties du moteur ne sont jamais relayées : la bibliothèque affiche le texte sur stdout, et l'enveloppe envoie stdout et stderr vers `/dev/null`.
6. **Hors ligne** :
   - Variables `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` et `HF_DATASETS_OFFLINE` forcées.
   - Dans le processus de l'enveloppe, connexions IPv4/IPv6 et DNS refusées.
   - Les poids SILMA sont résolus sous `SILMA_MODEL_DIR` et jamais par `cached_path`. Le vocodeur est chargé depuis un chemin local. CATT est chargé depuis `catt/*.onnx`, ce qui empêche son téléchargement automatique.
   - La transcription de la référence est toujours fournie, donc la transcription automatique (Whisper) n'est jamais chargée.
   - Un fichier manquant donne une erreur, jamais un téléchargement.
7. **VRAM 8 Go, un seul modèle lourd à la fois** : on suppose que SILMA utilise le GPU. L'étape `voix` passe sous `GPU_LOCK`. Avant elle, ComfyUI reçoit `/free` et Ollama est déchargé. Le moteur est arrêté avec son groupe de processus avant le retour de l'étape, donc avant l'image.

## Étape 0 : licences (vérifiées à la source le 26/11, sans télécharger de poids)

Composants nécessaires à l'exécution. La résolution complète (158 paquets, linux x86_64, Python 3.11, `uv pip compile silma-tts==1.0.5`) a été relue paquet par paquet sur PyPI. Seuls les composants significatifs et les exceptions figurent ci-dessous.

| Composant | Version | Licence | Source |
|---|---|---|---|
| Poids SILMA TTS v1 (`model.pt`, `vocab.txt`, `config.yaml`) | dépôt HF `226dd7a6` (23/08/2026) | Apache-2.0 ; entraîné de zéro, non dérivé des poids F5-TTS | https://huggingface.co/silma-ai/silma-tts |
| Paquet `silma-tts` (code dérivé de F5-TTS, sans dépendance au paquet `f5-tts`) | 1.0.5 (GitHub `409fb713`) | MIT | https://pypi.org/project/silma-tts/1.0.5/ , https://github.com/SILMA-AI/silma-tts |
| Vocodeur `charactr/vocos-mel-24khz` (`MelSpectrogramFeatures`) | HF main | MIT | https://huggingface.co/charactr/vocos-mel-24khz |
| `vocos` | 0.1.0 | MIT | https://github.com/charactr-platform/vocos |
| `encodec` (importé par `vocos`, poids **non utilisés** : le vocodeur est mel) | **commit `0e2d0aed29362c8e8f52494baf3e6f99056b214f`** (main, 0.1.2a3), pas le 0.1.1 de PyPI | MIT depuis le commit `349b7293` du 26/04/2023 ; 0.1.1 sur PyPI : CC-BY-NC-4.0 | https://github.com/facebookresearch/encodec/blob/0e2d0aed29362c8e8f52494baf3e6f99056b214f/LICENSE |
| Tashkeel CATT (`catt_tashkeel`, code) | 1.0.2 | Apache-2.0 | https://pypi.org/project/catt-tashkeel/1.0.2/ |
| Poids CATT `eo_model_onnx.zip` / `ed_model_onnx.zip` | release v2, publiés le 18/07/2025 à 03:47 | Apache-2.0 : passage de CC-BY-NC à Apache le 18/07/2025 à 00:25, avant la publication | https://github.com/abjadai/catt (README « License Change ») |
| Normalisation `nemo_text_processing` | 1.1.0 | Apache-2.0 | https://github.com/NVIDIA/NeMo-text-processing |
| `pynini` | 2.1.6.post1 | Apache-2.0 | https://github.com/kylebgorman/pynini |
| torch, torchaudio, torchcodec | 2.15 / 2.11 / 0.17 | BSD et Apache (expression SPDX de torch) | PyPI |
| transformers, huggingface-hub, cached-path, gradio | 5.24 / 1.33 / 1.8 / 6.32 | Apache-2.0 | PyPI |
| `onnxruntime-gpu` | 1.32 | MIT | PyPI |
| `unidecode` (exigé par silma-tts ; non importé par les fichiers relus de `silma_tts`) | 1.4.0 | **GPL-2.0+** : **exception acceptée par le parent le 26/11, limitée à ce paquet** | https://pypi.org/project/Unidecode/1.4.0/ |
| `frozendict` (via einx), `soxr` (via librosa) | 2.5.1 / 1.1.0 | LGPL-3.0, LGPL-2.1+ : **non tranché, attend l'utilisateur** | PyPI |
| certifi, tqdm, orjson | – | MPL-2.0 (en partie) : **non tranché, attend l'utilisateur** (certifi et tqdm sont déjà dans `uv.lock`, sans décision explicite) | PyPI |
| Bibliothèques CUDA NVIDIA (`nvidia-*`, `cuda-toolkit`) | 13.x | licence NVIDIA propriétaire (gratuite) : **non tranché, attend l'utilisateur** (aussi utilisées par ComfyUI sur le PC) | PyPI |
| Transcription automatique `openai/whisper-large-v3-turbo` | – | **non utilisée** : transcription toujours fournie | – |
| Voix d'exemple du paquet (`ar.ref.24k.wav`, `silma-ref-voice-commercial.wav`) | – | aucune licence propre : **non utilisées** | – |

Restent à vérifier au ticket d'installation, sans affirmation ici :

- la compatibilité de `vocos` 0.1.0 avec `encodec` 0.1.2a3 : vocos épingle `encodec==0.1.1`, il faudra donc installer encodec depuis le commit épinglé avec une contrainte qui remplace cette version ;
- l'absence de données téléchargées par NeMo à la construction des grammaires (la garde réseau la ferait échouer, jamais télécharger) ;
- la disposition exacte des poids CATT extraits.

## Disposition de `SILMA_MODEL_DIR`

`silma-tts/model.pt`, `silma-tts/vocab.txt`, `vocos-mel-24khz/config.yaml`, `vocos-mel-24khz/pytorch_model.bin`, `catt/encoder.onnx`, `catt/decoder.onnx`. L'enveloppe vérifie ces fichiers avant d'importer `silma_tts`.

## Conséquences

- La livraison de #249 est validée avec un faux moteur et une fausse bibliothèque seulement. **L'enveloppe réelle n'est pas validée sur le vrai moteur.** L'installation, le téléchargement des poids et l'essai en darija font l'objet d'un ticket ou d'un accord distinct.
- Un processus par réplique : le modèle et les grammaires NeMo sont rechargés à chaque fois. Le délai est de 600 s par réplique. Si c'est trop lent, un mode par lot de l'enveloppe fera l'objet d'un ticket distinct.
- La garde réseau de l'enveloppe agit au niveau Python. Une extension native pourrait la contourner : au ticket d'installation, ajouter une isolation réseau du processus sur le PC (pare-feu ou espace de noms réseau).
- Voix de référence : attend le parent (une par personnage, une pour la voix off ; adultes consentants ; jamais la voix de l'enfant sans décision expresse), et toujours hors dépôt.
- Licences : le parent a accepté seulement deux écarts à la ligne directrice « Apache 2.0, MIT » d'ARCHITECTURE.md : l'exception unidecode (GPL-2.0+), et encodec pris depuis le code passé en MIT. **Attend l'utilisateur : accepter les copyleft faibles LGPL (frozendict, soxr) et MPL (certifi, tqdm, orjson), et les bibliothèques CUDA NVIDIA propriétaires ?** Tant que ce n'est pas tranché, pas d'installation de SILMA.
