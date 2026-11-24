# ADR-001 — Architecture générale

- Statut : accepté — 2026-11-18
- Source : `docs/ARCHITECTURE.md` (document de référence détaillé)

## Contexte
Appli familiale : un enfant dicte (darija, français…) une scène à une IA conteuse et reçoit une
vidéo animée 9:16 (plans animés, voix off, sous-titres, musique). Contraintes : **0 € de frais
récurrents**, modèles et outils **open source** à licence permissive, usage par des enfants
(filtrage gradué, contrôle parental a posteriori, galerie privée). L'abonnement Claude sert à
construire le projet, jamais à le faire tourner.

## Décision
Architecture **hybride à deux machines**, reliées par un tunnel WireGuard (le PC n'est jamais exposé) :

| Nœud | Rôle | Composants |
|---|---|---|
| VPS (2 vCPU, 8 Go, sans GPU) | toujours allumé : interface, orchestration, file, montage, galerie, dashboard | Open WebUI, n8n (existant), `apps/media-service`, `apps/dashboard`, reverse proxy existant |
| PC GPU (RTX 4060 Ti 8 Go) | tout le calcul IA, étapes GPU **séquentielles** | Ollama + Qwen3 8B, ComfyUI (Flux schnell, Wan 2.2 5B), TTS, `apps/gpu-worker` |
| Mac | développement | Claude Code, agents |

Pipeline : conteur → storyboard JSON (`contracts/`) → classification libre/adouci/bloqué →
voix off → image clé → clip → montage FFmpeg → galerie. n8n orchestre ; les services exposent
des contrats versionnés dans `contracts/`.

Monorepo : `apps/*` (services), `contracts/` (schémas JSON), `prompts/` (prompts versionnés),
`comfyui/workflows/` (workflows API), `n8n/workflows/` (exports), `evals/` (évaluations),
`infra/` (déploiement, runner), `docs/adr/`.

> Mise à jour (ADR-004) : le déploiement et le runner `vps-deploy` vivent désormais dans le dépôt A ; B ne déploie plus.

Livraison : GitHub Actions (runners hébergés gratuits pour la CI, runner auto-hébergé sur le VPS
pour le déploiement, plus tard sur le PC pour les tâches GPU), images sur GHCR.

## Conséquences
- \+ Coût nul hors matériel existant ; données des enfants sur nos machines.
- \+ Contrats explicites entre VPS et PC : chaque service testable seul.
- − La génération dépend du PC allumé (Wake-on-LAN, décision en attente).
- − 8 Go de VRAM : une seule étape GPU à la fois, débit limité.
