# Run réel sur le PC GPU (ticket #239)

Étape 0 : inventaire et protocole. Aucune mesure n'a encore été faite ; le PC GPU reste en veille jusqu'au feu vert du ticket.
Aucune donnée d'enfant : prompts et entrées de test synthétiques uniquement ; fichiers de test supprimés après essai.

## Inventaire du PC GPU (25/11, lecture seule)

GPU : RTX 4060 Ti, 8188 MiB de VRAM (126 MiB au repos), driver 580.178.04.

| Brique | État | Détail |
|---|---|---|
| Flux schnell | présent | `flux1-schnell-Q4_K_S.gguf` (6,5 Go, Apache 2.0), VAE `ae.safetensors`, `clip_l`, `t5xxl_fp8_e4m3fn` |
| Wan 2.2 I2V A14B | présent | HighNoise + LowNoise `Q4_K_S.gguf` (8,3 Go chacun), LoRA lightning high/low, `wan2.2_vae`, `umt5_xxl_fp8_e4m3fn_scaled` |
| Wan 2.2 TI2V 5B | présent | `Wan2.2-TI2V-5B-Q8_0.gguf` (5,1 Go), couvre aussi le T2V |
| Wan 2.1 I2V 14B 480p | présent | `Q4_K_M.gguf` + LoRA lightx2v 480p |
| Wan 2.2 T2V A14B | manquant | non requis : TI2V-5B le remplace |
| Ollama | présent | `qwen3:8b` (5,2 Go), seul modèle |
| Piper | abandonné (#249) | `~/vva/tts/.venv/bin/piper` 1.8.0, plus utilisé par le pipeline |
| SILMA TTS | manquant | à installer dans un venv séparé (ticket d'installation, ADR-006) : `silma-tts` 1.0.5, `encodec` depuis le commit MIT épinglé, poids sous `SILMA_MODEL_DIR` |
| Voix de référence | manquant | attend le parent (une par personnage, une voix off) ; hors dépôt, déclarées dans `SILMA_VOIX` |
| Custom nodes | présents | ComfyUI-GGUF, comfyui-manager |

## Blocage

~~Voix Piper arabe absente~~ : tranché le 26/11, moteur SILMA TTS (#249, ADR-006). Reste avant un run réel : installation de SILMA sur le PC (ticket distinct) et voix de référence (attend le parent).

## Blocage GPU du 25/11 (étape 1, aucune mesure possible)

Après le réveil (`pc-gpu wake`, ~6 s), `comfyui.service` est en boucle de redémarrage : `RuntimeError: CUDA unknown error` à l'init de torch (`torch.cuda.is_available()` = False, alors que `nvidia-smi` voit la RTX 4060 Ti). Cause probable : état CUDA/nvidia_uvm cassé après la reprise de veille. Correctif possible (hors mandat, sudo requis) : recharger `nvidia_uvm` ou redémarrer le PC. Rien n'a été relancé ni modifié sur le PC. Attend le parent : redémarrage du PC ou accord pour `sudo rmmod nvidia_uvm && sudo modprobe nvidia_uvm`.

Préparé : `comfyui/workflows/wan22_a14b_i2v_lightning_api.json` (A14B Q4_K_S High/Low + lightning, 4 étapes, 480x864, 81 images, 24 i/s, sans RIFE), dérivé du workflow de labo `~/vva/lab`. Non testé tant que CUDA est KO.

## Contraintes VRAM 8 Go

- Un seul modèle lourd résident à la fois : décharger Ollama (`keep_alive: 0`) avant Flux, libérer ComfyUI (`/free`) avant Wan ; avant la voix SILMA (sous le verrou GPU), `/free` puis déchargement d'Ollama, et fin du processus du moteur avant l'image.
- Wan 2.2 A14B : High puis Low en séquence, jamais ensemble ; sinon repli sur TI2V-5B.
- Vidéo : 9:16 vertical, 24 i/s, longueur en 4n+1 images (ex. 49, 81).

## Protocole par étape

Ordre : Ollama, Flux, Wan 2.2, TTS (SILMA), FFmpeg. Pour chaque étape : `nvidia-smi` au repos, lancer, échantillonner la VRAM (`nvidia-smi --query-gpu=memory.used --format=csv -l 1`) pendant l'exécution, noter durée et pic, décharger, vérifier le retour au repos avant la suivante. Seed fixe notée.

1. Ollama (`qwen3:8b`) : un script court synthétique, sortie JSON ; relever tokens/s.
2. Flux schnell Q4_K_S (`comfyui/workflows/flux_schnell_t2i_api.json`) : image 9:16 (ex. 576x1024), 4 étapes, seed fixe.
3. Wan 2.2 (`comfyui/workflows/wan22_5b_i2v_api.json`, puis A14B si la VRAM tient) : image de l'étape 2 vers clip, 4n+1 images à 24 i/s ; comparer TI2V-5B et A14B lightning.
4. TTS SILMA (`SILMA_BIN` = enveloppe `silma_cli.py`, `SILMA_MODEL_DIR`, `SILMA_VOIX`) : trois répliques synthétiques alternant deux voix de référence d'adultes consentants, une en arabe standard, une en darija ; relever durée par réplique (chargement du modèle compris, un processus par réplique), pic de VRAM, et vérifier qu'aucune connexion réseau ne part (mode hors ligne). Qualité de la darija jugée à l'oreille par le parent.
5. FFmpeg : assemblage clip + voix + sous-titres ASS ; relever durée et taille.

## Grille de mesures (à remplir)

| Étape | Modèle | Résolution / durée | Étapes | Seed | Durée (s) | VRAM max (Mo) | Notes |
|---|---|---|---|---|---|---|---|
| Ollama | qwen3:8b | n/a | n/a | | | | |
| Flux | schnell Q4_K_S | | 4 | | | | |
| Wan 2.2 | TI2V-5B Q8_0 | | | | | | |
| Wan 2.2 | I2V A14B Q4_K_S | | | | | | |
| TTS | SILMA TTS v1 | n/a | n/a | | | | darija non annoncée |
| FFmpeg | n/a | | n/a | n/a | | | |

Critère de fin : toutes les lignes remplies, aucun OOM, fichiers de test supprimés, PC remis en veille si aucun job.
