# Mémoire des agents — conventions

Objectif : éviter que les agents relisent le dépôt à chaque session (moins de tokens), sans
stocker de donnée sensible. Décision : [ADR-003](adr/ADR-003-memoire-agents.md).

## Où écrire quoi

| Nature | Destination |
|---|---|
| Décision d'architecture | ADR versionné dans `docs/adr/` (jamais la mémoire) |
| Fait stable d'environnement (port, commande, chemin, quirk d'outil), préférence du parent | Mémoire **locale, hors dépôt** |
| État d'un ticket | Issue GitHub / Project (jamais la mémoire) |

## Format d'un fait

Un fichier par fait, **≤ 10 lignes** : titre, date, source, date d'expiration. Modèle :
[`tools/agents/memory/modele-fait.md`](../tools/agents/memory/modele-fait.md).

## Durée de vie

- Expiration par défaut : **90 jours** après la date du fait.
- À l'expiration : revérifier à la source, puis prolonger (nouvelle date) ou supprimer.
- Un fait reconnu obsolète est supprimé immédiatement.

## Interdit en mémoire

Secrets (clés, tokens, mots de passe), prompts, dictées, données d'enfant, tout contenu de
conversation. Doute sur un contenu : ne pas le mémoriser.

## Scénarios

- **Décision** : un agent qui tranche une question d'architecture crée un ADR, pas une entrée de mémoire.
- **Fait d'environnement** : écrit selon le modèle, avec expiration, hors du dépôt.
- **Donnée interdite** : un contenu qui ressemble à un secret ou à une donnée d'enfant n'est pas mémorisé.

## Hors périmètre

Base vectorielle ou service externe, mémoire du contenu des conversations, dashboard.

## Règle de dépôt

Aucun fichier `.claude/` ni `CLAUDE.md` réel n'est versionné (test `test_no_claude_tracked.py`). Le
modèle de référence est `tools/agents/CLAUDE.template.md`.
