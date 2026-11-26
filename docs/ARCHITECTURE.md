# Architecture — Viral Video Agents

> Document de référence du projet. Dernière mise à jour : 2026-11-18.

## 1. Objectif

Une **appli familiale** où les enfants **discutent avec une IA conteuse** (comme avec
ChatGPT ou Claude), dans leur langue — français, arabe, darija… — pour inventer une
histoire, puis reçoivent une **vraie vidéo animée** : plans animés, voix off,
sous-titres, musique, au format vertical 9:16.

## 2. Contraintes non négociables

| Contrainte | Conséquence |
|---|---|
| **0 € de frais en plus** | Aucune API payante (pas d'Anthropic API, fal.ai, ElevenLabs, Kling, Veo…) |
| **100 % open source / gratuit** | Licences permissives (Apache 2.0, MIT) pour rester libre même en cas de monétisation |
| **Utilisée par des enfants** | Comptes par enfant, imagination non bridée, filtrage gradué (libre / adouci / bloqué), **vérification parentale a posteriori**, galerie privée, aucune publication automatique |
| **L'abonnement Claude** | Sert à **construire** le projet (Claude Code), **pas** à faire tourner l'appli : un compte grand public est personnel et réservé aux 18 ans et plus |
| **Espace disque** | Les fichiers de test sont supprimés après chaque essai ; seuls les modèles restent |

## 3. Matériel

| Machine | Caractéristiques | Rôle |
|---|---|---|
| **VPS Hostinger** (KVM 2) | 2 vCPU, 8 Go RAM, 100 Go, pas de GPU, Ubuntu 24.04, **n8n déjà installé** | Toujours allumé : interface, orchestration, file d'attente, montage, galerie |
| **PC gamer** `192.168.1.14` (`fchebihi`) | RTX 4060 Ti **8 Go VRAM**, 62 Go RAM, Ubuntu 22.04, pilote NVIDIA 580, CUDA 13 | Tout le calcul IA (LLM, images, vidéo) |
| **Mac** | Poste de développement | Code, tests, Claude Code |

> ⚠️ Secure Boot est actif sur le PC : le pilote NVIDIA doit utiliser les modules
> **signés par Canonical** (`kernel/nvidia-580`). Si `dkms status` liste à nouveau
> `nvidia` après une mise à jour, l'écran peut se figer → supprimer la copie DKMS.

## 4. Parcours utilisateur

```
Enfant ──▶ Open WebUI (VPS) ◀──▶ Qwen3 « studio » (PC)
   dicte toute sa scène (darija, voix ou texte)
   ◀── récap trilingue + bouton ✅ (pas de questions)
                │  outil « créer la vidéo »
                ▼
              n8n (VPS) ──▶ classification : libre / adouci / bloqué
                │                         └──▶ rapport parent (a posteriori)
                ▼
   PC : images (Flux / Z-Image) ─▶ clips (Wan 2.2) ; voix off ; sous-titres
                │
                ▼
   VPS : montage FFmpeg ─▶ galerie privée ─▶ lien renvoyé dans la conversation
```

## 5. Composants

### Sur le PC gamer (installé le 2026-11-18, services `systemctl --user`)

| Composant | Rôle | Détails |
|---|---|---|
| **Ollama** 0.40 | Serveur LLM | `~/.local/ollama`, service `ollama`, `127.0.0.1:11434` |
| **Qwen3 8B** | Conteur, scénariste, storyboard | Apache 2.0, ~52 tokens/s, 5,5 Go VRAM |
| **ComfyUI** 0.45 | Moteur image/vidéo + API | `~/ComfyUI`, Python 3.13, PyTorch 2.14 cu130, service `comfyui`, `127.0.0.1:8188` |
| ComfyUI-Manager, ComfyUI-GGUF | Extensions | GGUF = modèles compressés pour 8 Go |
| **Flux.1 schnell** Q4 | Images clés | Apache 2.0, 4 étapes, ~13 s par image 768×1344 |
| **Wan 2.2 TI2V 5B** Q8 | Image → vidéo | Apache 2.0 |
| **SILMA TTS** v1 (150M, poids Apache 2.0) | Voix par réplique (personnages, voix off), clonage depuis une référence < 8 s | Choisi le 26/11 (#249, ADR-006) ; **pas encore installé**, qualité en darija non testée ; enveloppe `apps/media-service/scripts/silma_cli.py`, sous le verrou GPU. Piper abandonné |

### Sur le VPS (à faire)

| Composant | Rôle |
|---|---|
| **Open WebUI** | Interface de discussion type ChatGPT : comptes enfants, micro, assistant « conteur magique », outil « créer la vidéo » |
| **n8n** (existant) | Orchestration du pipeline, validation parent via Telegram, relances |
| **media-service** (Python) | Montage FFmpeg, sous-titres, mixage, galerie |
| **WireGuard** | Tunnel privé VPS ↔ PC (le PC n'est jamais exposé à Internet) |

## 6. Pipeline de génération

1. **Conteur** (Qwen3) : discute avec l'enfant, produit l'histoire finale.
2. **Storyboard** (Qwen3, sortie JSON) : découpage en plans, prompts image et mouvement, texte de voix off par plan.
3. **Classification du contenu** (pas de validation préalable : le parent vérifie *après*) :
   - **Libre** : tout l'imaginaire, y compris des combats façon jeu vidéo entre animaux.
   - **Adouci** : la violence est transformée en version dessin animé (pas de sang, pas de mort ; lasers, bulles de peinture, « pouf » de fumée), avec un petit mot gentil à l'enfant.
   - **Bloqué** : contenu sexuel ou nudité → message bienveillant, rien n'est généré.
   - **Signalé au parent** : tentatives adoucies ou bloquées, sujets tabous, signes de détresse. Ces signalements alimentent le rapport parent (historique, dictée originale avec traduction, ce qui a été modifié).
4. **Voix off** par plan → la durée de la voix fixe la durée du plan.
5. **Image clé** par plan (Flux / Z-Image).
6. **Clip** par plan : Wan 2.2 anime l'image clé (24 i/s, longueur en 4n+1 images).
7. **Montage** : concaténation, sous-titres mot par mot, musique avec ducking, 1080×1920.

La carte n'a que 8 Go : les étapes GPU s'enchaînent une par une (le script décharge Ollama avant chaque génération ComfyUI).

## 7. Fichiers du dépôt

| Chemin | Contenu |
|---|---|
| `comfyui/workflows/flux_schnell_t2i_api.json` | Workflow ComfyUI texte → image (marqueur `{{PROMPT}}`) |
| `comfyui/workflows/wan22_5b_i2v_api.json` | Workflow ComfyUI image → vidéo (`{{PROMPT}}`, `{{IMAGE}}`) |
| `apps/gpu-worker/comfy_run.py` | Lance un workflow via l'API ComfyUI et récupère les fichiers (bibliothèque standard uniquement) |

Sur le PC, ces fichiers sont copiés dans `~/vva/`.

## 8. Décisions en attente

- Âge des enfants (ton et règles du conteur).
- ~~PC éteint~~ : tranché le 19/11 (#37), voir « Alimentation du PC GPU » ci-dessous.
- Modèle d'image : comparer Flux schnell et **Z-Image-Turbo**.
- Voix : SILMA TTS retenu pour un essai (ADR-006, #249) ; la darija n'est pas annoncée par sa fiche : qualité à juger à l'oreille au ticket d'installation. Voix de référence (une par personnage, une pour la voix off) : attend le parent.
- Darija : la conversation l'accepte ; reconnaissance vocale et voix off en darija restent à évaluer.

**Alimentation du PC GPU (#37, décision du parent 19/11).**

- Réveil à la demande : `GpuPower.ensure_awake()` appelle `pc-gpu wake` seulement si `status` n'est pas « éveillé », puis rend la main PC éveillé ; échec ou plus de 120 s : `GpuUnavailable`, sans boucle.
- Veille 5 à 10 min après le dernier job (`idle_s`, 300–600 s, défaut 600) : `tick()` n'appelle `pc-gpu sleep` que si aucun job ne tourne, si `status` dit « éveillé » et si `idle_s` s'est écoulé depuis le dernier `job_finished`.
- Doute (statut illisible, en erreur ou ambigu) : on n'endort pas ; l'erreur est journalisée sans texte d'enfant.
- Seule interface vers le PC : `~/.local/bin/pc-gpu wake|sleep|status` (arguments en liste, sans shell), depuis le réseau local seulement (#158) ; ni VPN, ni conteur de secours, ni plage horaire.
- Pire cas de blocage de `ensure_awake()` : 3 appels à `pc-gpu` (`status`, `wake`, `status`) de 120 s au plus chacun, soit 360 s ; l'appelant doit l'exécuter hors de la boucle de requêtes (thread ou tâche) et afficher « j'allume le PC » à l'enfant.
- ~~Décision ouverte (instancie qui ?)~~ : tranché (#223) : le compteur vit côté Mac relais, dans l'appli enfant (`apps/kid-ui`), jamais sur le PC GPU, qui ne peut pas se réveiller seul. Avec `--gpu-power`, `JobStore` appelle `ensure_awake()` dans le fil du job avant le CLI (statut `queued` + `waking` : « j'allume le PC »), encadre le job par `job_started/job_finished`, et `jobs.Veille` appelle `tick()` toutes les 30 s dans un fil démon arrêtable (arrêté à la sortie du serveur ; une exception de `tick()` est journalisée par son nom seul). Un verrou sérialise `ensure_awake()` et la fin de `tick()` (dernier contrôle + `sleep`) : une dictée arrivée pendant un `sleep` attend sa fin avant de lire `status` (pire cas porté à 480 s). `GpuUnavailable` : job en `error`, seul le nom de l'exception est journalisé. Sans l'option : aucun appel à `pc-gpu`.
- `worker.py` reçoit la minuterie par injection (`JobQueue(..., power=)`, défaut sans effet) ; code : `apps/gpu-worker/gpu_power.py`.

## 9. Feuille de route

| Phase | Livrable |
|---|---|
| P0 ✅ | PC réparé, Ollama + ComfyUI + Flux opérationnels |
| P1 (en cours) | Première vidéo test : 2 plans animés + voix off + sous-titres |
| P2 | Choix définitif des modèles (image, voix) |
| P3 | VPS : Open WebUI + conteur, WireGuard, n8n relié au PC |
| P4 | media-service + galerie + validation Telegram |
| P5 | Musique, darija, améliorations qualité |
