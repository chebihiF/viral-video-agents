# Métriques de tokens (OTLP/HTTP JSON)

`POST /v1/metrics` — reçoit la télémétrie OpenTelemetry de Claude Code (Mac) et ne garde
que les compteurs de tokens. Réglages côté Mac : [`tools/telemetry/README.md`](../tools/telemetry/README.md).

## Transport

- OTLP/HTTP, encodage **JSON** (`Content-Type: application/json`, protocole `http/json`).
  Le protobuf n'est pas accepté (415), la compression non plus (corps lu tel quel).
- Authentification identique à [`ingest-api.md`](ingest-api.md) : `Authorization: Bearer
  <INGEST_TOKEN>`, temps constant, service fermé (503) sans `INGEST_TOKEN`, vérifiée avant
  toute lecture du corps.
- Corps limité à **256 Ko** (`Content-Length` puis flux réel), comme `/api/v1/events`.

## Données retenues

Seule la métrique `claude_code.token.usage` (type `sum`) est lue ; toutes les autres
métriques du lot sont ignorées. Seule la temporalité **delta** (`aggregationTemporality: 1`)
est acceptée : un point cumulatif ferait double compte, il est rejeté.

### Attributs retenus (liste blanche stricte)

Lus parmi les attributs de la ressource puis du point (le point l'emporte), valeur
`stringValue` uniquement. **Tout autre attribut est ignoré et jamais stocké** (dont
`user.email`, `user.id`, `user.account_uuid`, `organization.id`, `terminal.type`…).
Aucun prompt ni texte ne peut atteindre la base.

| Attribut OTLP | Colonne | Obligatoire | Contrainte |
|---------------|---------|-------------|------------|
| `session.id` | `session_id` | oui | 1 à 128 caractères |
| `agent.name` | `agent_id` | non | 1 à 128 caractères |
| `model` | `model` | oui | 1 à 128 caractères |
| `type` | `type` | oui | `input`, `output`, `cacheRead`, `cacheCreation` |

Valeur du point : `asInt` (entier ou chaîne décimale) ou `asDouble` (arrondi), fini, entre 0
et **10^12** inclus (au-delà : point rejeté, protège les sommes SQLite int64).
`timeUnixNano` (fin d'intervalle, > 0) devient `ts` (ISO 8601 UTC, microsecondes, `+00:00`) ;
un point daté de plus d'**1 h dans le futur** (horloge serveur) est rejeté.

### Attribution par agent

Constat sur Claude Code 2.1.327 (binaire inspecté, non vérifié en exécution) : le compteur
porte `session.id`, `model`, `type`, `query_source` et, pour un sous-agent, `agent.name`
(nom de l'agent, ex. `backend`). **Aucun identifiant d'instance de sous-agent** (l'`agent_id`
des hooks, cf. `agent-event.schema.json`) n'est émis. Donc :

- `token_usage.agent_id` contient le **nom** de l'agent (`agent.name`), pas l'identifiant
  d'instance des hooks : ne pas joindre `token_usage.agent_id` à `events.agent_id`.
- Sans `agent.name` (fil principal), `agent_id` est `NULL` et la ligne est rattachée à
  sa session par `session_id` ; `/tokens` l'affiche sous le libellé « (fil principal) »,
  distinct d'un agent qui s'appellerait `session`.

## Table `token_usage`

| Colonne | Type | Note |
|---------|------|------|
| `point_id` | TEXT PRIMARY KEY | clé d'idempotence (voir ci-dessous) |
| `ts` | TEXT NOT NULL | indexé |
| `session_id` | TEXT NOT NULL | |
| `agent_id` | TEXT NULL | nom de l'agent |
| `model` | TEXT NOT NULL | |
| `type` | TEXT NOT NULL | `input` \| `output` \| `cacheRead` \| `cacheCreation` |
| `count` | INTEGER NOT NULL | 0 à 10^12 |
| `received_at` | TEXT NOT NULL | horodatage serveur, même format que `ts` |

### Idempotence

`point_id` = SHA-256 de `[session_id, agent_id ou "", model, type, startTimeUnixNano,
timeUnixNano]`. En delta, un intervalle d'une série est unique : un lot réexporté (nouvelle
tentative de l'exportateur) produit les mêmes clés et est ignoré (`INSERT OR IGNORE`), les
totaux ne doublent pas. Deux points d'une même série au même intervalle seraient fusionnés
(cas impossible en delta).

## Réponses (ExportMetricsServiceResponse)

| Code | Cas | Corps |
|------|-----|-------|
| 200 | lot traité (nouveaux points et doublons compris) | `{}` |
| 200 | des points de tokens rejetés (attribut manquant, type inconnu, valeur > 10^12, ts > maintenant + 1 h, cumulatif…) ; les autres sont enregistrés | `{"partialSuccess": {"rejectedDataPoints": n, "errorMessage": "..."}}` |
| 400 | JSON invalide ou enveloppe OTLP mal formée ; rien n'est écrit | `{"detail": [...]}` |
| 401 | jeton absent ou faux | `{"detail": "..."}` |
| 413 | corps > 256 Ko | `{"detail": "..."}` |
| 415 | `Content-Type` autre que `application/json` | `{"detail": "..."}` |
| 503 | `INGEST_TOKEN` non configuré | `{"detail": "..."}` |

## Page `/tokens`

Totaux de tokens (tous types confondus) par agent et par modèle, sur 24 h et 7 jours
glissants. Ni identifiant de session ni attribut personnel n'y est affiché.
