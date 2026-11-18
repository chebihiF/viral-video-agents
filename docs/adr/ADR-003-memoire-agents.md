# ADR-003 — Mémoire des agents : mémoire locale vs ADR

- Statut : proposé — 2026-11-18

## Contexte
Les agents relisent le dépôt à chaque session, ce qui coûte des tokens. Une mémoire aide, mais
le projet manipule des données d'enfants et des secrets d'infrastructure : rien de sensible ne
doit y entrer, ni dans le dépôt.

## Options
1. **Mémoire locale hors dépôt pour les faits d'environnement, ADR versionné pour les décisions, issues pour l'état** — séparation nette, rien de sensible versionné.
2. Tout versionner dans le dépôt — risque de fuite, `.claude/` interdit en version.
3. Base vectorielle / service externe — surface technique et coût, hors périmètre.

## Décision
Option 1. Faits ≤ 10 lignes (titre, date, source), expiration à 90 jours par défaut, suppression
des faits obsolètes. Interdits : secrets, prompts, dictées, données d'enfant, contenu de
conversation. Détails : `docs/agents-memory.md`.

## Conséquences
- \+ Moins de relectures du dépôt ; décisions traçables dans les ADR.
- \+ Aucune donnée sensible versionnée.
- − Mémoire locale non partagée entre machines ; discipline de revue des expirations requise.
