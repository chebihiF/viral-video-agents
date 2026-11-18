# Télémétrie des agents (émetteur Mac, #4)

Trois scripts **stdlib uniquement**, compatibles **Python 3.9** (le `python3` du Mac, celui que
les hooks appellent ; pas de `datetime.UTC` ni de `zip(strict=)`, testé sous 3.9 et 3.12) :

- `hook_emit.py` : appelé par les hooks Claude Code, lit le JSON du hook sur stdin et
  ajoute **une ligne** conforme à [`contracts/agent-event.schema.json`](../../contracts/agent-event.schema.json)
  au fichier JSONL. Champs écrits : `event_id` (UUID), `ts` (UTC), `hook_event_name`,
  `session_id`, `agent_type`, `agent_id`, `tool_name`. **Jamais** de prompt, d'arguments
  ni de sortie d'outil. Toute erreur (ou plus de 0,8 s) : sortie en code 0, sans bruit.
- `ship.py` : envoie les lignes en attente vers `POST /api/v1/events`
  ([`contracts/ingest-api.md`](../../contracts/ingest-api.md)) par lots de 100 au plus.
- `pretool_gate.py` : hook `PreToolUse` qui bloque les outils quand les agents sont en pause
  (#8, [`contracts/control-api.md`](../../contracts/control-api.md)). Échec sûr : en cas de doute, il laisse passer.

## Variables d'environnement

| Variable | Rôle | Défaut |
|----------|------|--------|
| `VVA_TELEMETRY_FILE` | fichier JSONL | `$CLAUDE_PROJECT_DIR/.claude/telemetry/events.jsonl` |
| `VVA_INGEST_URL` | URL complète de l'ingestion : `https`, ou `http` sur localhost/127.0.0.1/::1 seulement | requis pour `ship.py` |
| `VVA_INGEST_TOKEN` | jeton Bearer (= `INGEST_TOKEN` du VPS) | requis pour `ship.py` |

Le jeton reste dans l'environnement local (trousseau, `~/.zshenv`…), jamais dans le dépôt.

## Câblage des hooks (à copier en local)

À placer dans **votre** `.claude/settings.local.json` (ou `~/.claude/settings.json`).
Ce fichier n'est **pas** versionné : seul cet exemple l'est. Les variables `VVA_*` doivent être
visibles des hooks (bloc `"env"` du même fichier, ou `~/.zshenv`).

- `SubagentStart`, `SubagentStop`, `PostToolUse` : `hook_emit.py` écrit une ligne par événement.
- `PreToolUse` : `pretool_gate.py` applique la pause (`VVA_CONTROL_URL`, `VVA_INGEST_TOKEN`).
- `Stop` : `ship.py --background` envoie les lignes en attente à chaque fin de tour (le verrou évite les envois concurrents).
  Il rend la main aussitôt (code 0, aucune sortie, stdin ignoré), puis expédie dans un
  processus détaché (nouvelle session, timeout réseau de 3 s). Sans `VVA_INGEST_URL` /
  `VVA_INGEST_TOKEN`, il sort en silence. Aucun `launchd` nécessaire ; le verrou
  `<fichier>.lock` évite deux expéditeurs simultanés.

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
    ],
    "PreToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "timeout": 5,
        "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/telemetry/pretool_gate.py\"" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "timeout": 2,
        "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/telemetry/ship.py\" --background" }] }
    ]
  }
}
```

Vérification : `echo '{"hook_event_name":"Stop","session_id":"t"}' | python3 tools/telemetry/hook_emit.py`
ajoute une ligne à `events.jsonl` (sans erreur sous le `python3` 3.9 du Mac).

## Ancien fichier (`log_event.sh`)

Au démarrage, `ship.py` examine les lignes en attente : une ligne JSON sans `event_id`, `ts`,
`hook_event_name` ni `session_id` est de l'ancien format. Le fichier est alors renommé en
`events.legacy-AAAAMMJJ.jsonl` (suffixe `-2`, `-3`… si l'archive existe déjà), le curseur est
supprimé et **rien** de l'archive n'est jamais renvoyé. Les lignes valides en attente sont
reprises dans un nouveau `events.jsonl`. Les archives peuvent être supprimées à la main.

## Envoi

```sh
VVA_INGEST_URL=https://<dashboard>/api/v1/events VVA_INGEST_TOKEN=... \
  python3 tools/telemetry/ship.py
```

À la main, ou automatiquement par le hook `Stop` (`--background`, voir ci-dessus). Codes de sortie :
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

## Tokens par agent et par modèle (OpenTelemetry, #7)

Claude Code exporte lui-même ses compteurs de tokens en OTLP ; aucun script ici. Le
dashboard ne garde que `claude_code.token.usage` et une liste blanche d'attributs
([`contracts/token-usage.md`](../../contracts/token-usage.md)). À placer dans le bloc
`"env"` de **votre** `~/.claude/settings.json` (ou exporter dans le shell), jamais versionné :

| Variable | Valeur | Pourquoi |
|----------|--------|----------|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` | active la télémétrie |
| `OTEL_METRICS_EXPORTER` | `otlp` | métriques uniquement |
| `OTEL_LOGS_EXPORTER` | non défini (ou `none`) | les événements/logs peuvent contenir du texte ; le dashboard ne les reçoit pas |
| `OTEL_EXPORTER_OTLP_METRICS_PROTOCOL` | `http/json` | le dashboard n'accepte pas le protobuf (415) |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | `https://<dashboard>/v1/metrics` | URL complète, utilisée telle quelle |
| `OTEL_EXPORTER_OTLP_METRICS_HEADERS` | `Authorization=Bearer <VVA_INGEST_TOKEN>` | même jeton que l'ingestion |
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | `delta` | les points cumulatifs sont rejetés (double compte) |
| `OTEL_METRIC_EXPORT_INTERVAL` | `60000` (défaut) | un envoi par minute |
| `OTEL_METRICS_INCLUDE_SESSION_ID` | `true` (défaut) | requis : rattachement par session |
| `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` | `false` | moins d'identifiants envoyés (de toute façon ignorés) |
| `OTEL_LOG_USER_PROMPTS` | non défini ou `0` | **désactivé** : aucun prompt exporté |
| `OTEL_LOG_TOOL_DETAILS` | non défini ou `0` | aucun détail d'outil exporté |

Ne pas définir `OTEL_EXPORTER_OTLP_COMPRESSION` (corps lu non compressé). Vérification :
ouvrir `https://<dashboard>/tokens` après une minute d'activité. Les tokens d'un
sous-agent portent son nom (`agent.name`) ; le fil principal apparaît sous « (fil principal) ».

## Compaction

Le JSONL n'est jamais réduit automatiquement. Une fois la file vidée (`ship.py` sort
en `0`), on peut le compacter : supprimer ou renommer `events.jsonl` (et son `.offset`)
pendant qu'aucun `ship.py` ne tourne. Les hooks recréent le fichier au prochain
événement ; le nouvel inode remet le curseur à 0. Surveiller aussi `<fichier>.rejected`,
qui grossit sans limite.
