# Télémétrie des agents (émetteur Mac, #4)

Deux scripts **stdlib uniquement** (Python ≥ 3.12) :

- `hook_emit.py` : appelé par les hooks Claude Code, lit le JSON du hook sur stdin et
  ajoute **une ligne** conforme à [`contracts/agent-event.schema.json`](../../contracts/agent-event.schema.json)
  au fichier JSONL. Champs écrits : `event_id` (UUID), `ts` (UTC), `hook_event_name`,
  `session_id`, `agent_type`, `agent_id`, `tool_name`. **Jamais** de prompt, d'arguments
  ni de sortie d'outil. Toute erreur (ou plus de 0,8 s) : sortie en code 0, sans bruit.
- `ship.py` : envoie les lignes en attente vers `POST /api/v1/events`
  ([`contracts/ingest-api.md`](../../contracts/ingest-api.md)) par lots de 100 au plus.

## Variables d'environnement

| Variable | Rôle | Défaut |
|----------|------|--------|
| `VVA_TELEMETRY_FILE` | fichier JSONL | `$CLAUDE_PROJECT_DIR/.claude/telemetry/events.jsonl` |
| `VVA_INGEST_URL` | URL complète de l'ingestion : `https`, ou `http` sur localhost/127.0.0.1/::1 seulement | requis pour `ship.py` |
| `VVA_INGEST_TOKEN` | jeton Bearer (= `INGEST_TOKEN` du VPS) | requis pour `ship.py` |

Le jeton reste dans l'environnement local (trousseau, `~/.zshenv`…), jamais dans le dépôt.

## Réglage des hooks (à copier en local)

À placer dans **votre** `.claude/settings.local.json` (ou `~/.claude/settings.json`).
Ce fichier n'est **pas** versionné : seul cet exemple l'est.

```json
{
  "hooks": {
    "SubagentStart": [
      { "hooks": [{ "type": "command", "timeout": 2,
        "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/telemetry/hook_emit.py\"" }] }
    ],
    "SubagentStop": [
      { "hooks": [{ "type": "command", "timeout": 2,
        "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/telemetry/hook_emit.py\"" }] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "timeout": 2,
        "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/telemetry/hook_emit.py\"" }] }
    ]
  }
}
```

## Envoi

```sh
VVA_INGEST_URL=https://<dashboard>/api/v1/events VVA_INGEST_TOKEN=... \
  python3 tools/telemetry/ship.py
```

À lancer périodiquement (ex. `launchd` toutes les minutes). Codes de sortie :
`0` file vidée, `1` serveur injoignable ou 5xx (file conservée, réessayer plus tard),
`2` configuration (variable manquante, URL refusée, redirection 3xx, 4xx hors 413/422 ;
file conservée).

## Fiabilité

- Curseur `<fichier>.offset` (inode + octets) : n'avance qu'après un `200`/`202`. Rien n'est
  perdu ; un renvoi éventuel est sans doublon grâce à `event_id` (idempotence serveur).
- Une ligne en cours d'écriture (sans `\n`) n'est pas envoyée.
- Aucune redirection n'est suivie (le jeton ne part jamais vers une autre URL).
- `413`/`422` sur un lot : renvoi événement par événement ; les refusés et les lignes
  illisibles sont isolés dans `<fichier>.rejected` sans bloquer la file.
- Fichiers d'état (`.offset`, `.rejected`, `.lock`) en `0600`.
- Verrou `<fichier>.lock` : un seul expéditeur à la fois.
- Fichier remplacé (inode différent) ou tronqué (offset > taille) : reprise depuis le
  début ; les renvois sont absorbés par la déduplication serveur.

## Compaction

Le JSONL n'est jamais réduit automatiquement. Une fois la file vidée (`ship.py` sort
en `0`), on peut le compacter : supprimer ou renommer `events.jsonl` (et son `.offset`)
pendant qu'aucun `ship.py` ne tourne. Les hooks recréent le fichier au prochain
événement ; le nouvel inode remet le curseur à 0. Surveiller aussi `<fichier>.rejected`,
qui grossit sans limite.
