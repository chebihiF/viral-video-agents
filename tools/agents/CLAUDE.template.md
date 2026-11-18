# Modèle de CLAUDE.md pour les agents (à copier hors du dépôt)

Ce fichier est un modèle générique. Le vrai `CLAUDE.md` et le dossier `.claude/` restent
locaux et ne sont jamais versionnés.

## Principes
- Répondre en français, de façon concise.
- Lire d'abord `docs/MAP.md` et le ticket concerné avant de parcourir le dépôt.
- Ne modifier que les fichiers listés dans le ticket.
- Travailler en TDD : un test qui échoue, puis le code minimal, puis refactor.

## Flux Git
- Une branche par ticket, nommée `feat/*`, `fix/*`, `chore/*`, `docs/*`, `test/*` ou `ci/*`.
- Messages de commit courts, à l'impératif, en français.
- Jamais de push sur `main`, jamais de force-push.
- Ne jamais ajouter au dépôt `CLAUDE.md`, `.claude/` ni fichier de mémoire.

## Qualité
- Lancer `make check` avant toute livraison.
- Aucune dépendance ajoutée sans ADR si elle change l'architecture.

## Mémoire
- Conventions : `docs/agents-memory.md`.
- Décision d'architecture : ADR dans `docs/adr/`.
- Fait d'environnement stable : mémoire locale hors dépôt, ≤ 10 lignes, avec date, source, expiration à 90 jours.
- État de ticket : issue GitHub.
- Ne jamais mémoriser : secrets, prompts, dictées, données d'enfant, contenu de conversation.

## Sécurité
- Ne jamais écrire de secret, clé ou jeton dans un fichier, un commit ou une issue.
- Ne rien exécuter qui touche à l'environnement local en cours d'usage sans accord du parent.
- Ne pas lire ni citer de données d'enfant.

## Rapport
- Rapport final court : ce qui est fait, ce qui reste, fichiers touchés.
