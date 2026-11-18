# ADR-002 — Stack du dashboard (et des services Python)

- Statut : accepté par le propriétaire du produit — 2026-11-18

## Contexte
Le dashboard montre l'activité des agents (sous-agents, outils, tokens par agent et par modèle,
sprint en cours) avec des contrôles (pause, plafond). Il tourne sur le VPS (2 vCPU, 8 Go) à côté
de n8n. Équipe : un humain + des agents Claude ; il faut minimiser la surface technique et les
tokens consommés pour la maintenir.

## Options
1. **Python 3.12 + FastAPI + Jinja2 + HTMX (+ SSE)** — un seul langage pour tout le dépôt, pas de build JS.
2. FastAPI + SPA React/Vite — UI plus riche, mais chaîne npm, double outillage, plus de code à relire.
3. Litestar + HTMX — équivalent à 1, écosystème plus petit.

## Décision
Option 1. Gestion des dépendances par **uv** (workspace à la racine, `uv.lock`), qualité par
**ruff** (lint + format), **mypy strict**, **pytest**. Serveur **uvicorn**, image Docker
multi-étapes non-root. Le temps réel passera par Server-Sent Events ; HTMX est servi en fichier
statique local (pas de CDN). Persistance prévue : SQLite (fichier sur volume) tant qu'un seul
nœud écrit.

## Conséquences
- \+ Même stack que `media-service` et les scripts GPU ; tests rapides ; image légère.
- \+ Rendu côté serveur : peu de JavaScript à maintenir.
- − Interactions très riches (graphes animés) limitées ; si besoin, îlots JS ciblés.
