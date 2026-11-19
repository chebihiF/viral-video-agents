# API d'ingestion des événements d'agents

`POST /api/v1/events` — reçoit les événements de hooks envoyés par le Mac (émetteur : #4).

## Authentification

En-tête `Authorization: Bearer <INGEST_TOKEN>`.

- Le jeton est lu depuis la variable d'environnement `INGEST_TOKEN` (jamais dans le dépôt).
- Comparaison en temps constant (`hmac.compare_digest`).
- `INGEST_TOKEN` absent ou vide : le service est **fermé** (503), aucun jeton n'est accepté.
- L'authentification est vérifiée **avant** toute lecture ou validation du corps.

## Corps

`application/json`, soit :

- un objet conforme à [`agent-event.schema.json`](agent-event.schema.json) ;
- soit un tableau de 1 à 100 objets conformes.

Taille maximale du corps : **256 Ko** (262 144 octets), contrôlée sur `Content-Length`
puis sur le flux réellement lu (lecture bornée). `ts` doit être une **chaîne** date-time
ISO 8601 avec fuseau (un nombre est refusé) ; il est stocké normalisé en UTC.

`ticket` (#100) est optionnel et rétrocompatible : entier JSON de 1 à 2 147 483 647 (`"93"`, `0`, `-1`,
`true` ou `9.5` sont refusés en 422), absent ou `null` hors branche de ticket. Le hook
d'envoi (`tools/telemetry/hook_emit.py`) le déduit de la branche git du `cwd` du hook
(`feat|fix|chore|docs|test|ci/<n>-…`, n de 1 à 9 chiffres, même motif que `tools/agents/token_report.py`) et
n'écrit que le nombre, jamais le nom de branche.

Tout champ hors schéma (ex. `prompt`) est refusé. Un lot est **atomique** : si un seul
événement est invalide, rien n'est écrit.

## Persistance

SQLite, fichier désigné par `VVA_DB_PATH`, table `events` (clé unique `event_id`,
index sur `ts` et `agent_id`, mode WAL, attente de verrou 5 s). Schéma créé au premier
accès. Un `event_id` déjà connu est ignoré (idempotence). Colonne `ticket INTEGER`
nullable : une base antérieure à #100 la reçoit par migration idempotente
(`ALTER TABLE … ADD COLUMN`), ses anciens événements restent à `NULL`.

Quota de tokens (#100, lu par `apps/dashboard/src/dashboard/quota.py`) : variables
`VVA_QUOTA_5H_TOKENS` (fenêtre glissante de 5 h) et `VVA_QUOTA_WEEK_TOKENS` (semaine),
entiers > 0 réglés par le parent. Aucune valeur par défaut : absente ou invalide =
« non configuré » (`None`).

## Réponses

| Code | Cas | Corps |
|------|-----|-------|
| 202 | au moins un événement nouveau enregistré | `{"inserted": n, "duplicates": m}` |
| 200 | tous les événements étaient déjà connus | `{"inserted": 0, "duplicates": m}` |
| 401 | jeton absent ou faux | `{"detail": "..."}` |
| 413 | corps > 256 Ko (vérifié après l'authentification) | `{"detail": "..."}` |
| 422 | JSON invalide, champ hors schéma, lot vide ou > 100 | `{"detail": [...]}` |
| 503 | `INGEST_TOKEN` non configuré | `{"detail": "..."}` |
