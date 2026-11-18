#!/usr/bin/env bash
# Déploie le dashboard sans coupure (blue/green derrière Traefik), vérifie /healthz en HTTPS,
# revient à la version précédente si échec. Slot actif mémorisé dans $STATE_DIR/slot.
# Usage : deploy.sh <image> <version>   (exécuté par le runner auto-hébergé du VPS)
set -euo pipefail

IMAGE="$1"
VERSION="$2"
STATE_DIR="${VVA_STATE_DIR:-/opt/vva}"
HERE="$(cd "$(dirname "$0")" && pwd)"

set -a
# shellcheck source=/dev/null
. "$STATE_DIR/proxy.env"
set +a
export IMAGE
# Jeton d'ingestion : généré une fois sur le VPS (jamais dans le dépôt), passé au conteneur par l'environnement.
"$HERE/ensure-token.sh" "$STATE_DIR"
INGEST_TOKEN="$(cat "$STATE_DIR/ingest_token")"
export INGEST_TOKEN
# Clé de session : générée une seule fois (conservée entre deux déploiements), jamais dans le dépôt.
if [ ! -s "$STATE_DIR/session_key" ]; then
  ( umask 077; openssl rand -hex 32 > "$STATE_DIR/session_key.tmp" && mv "$STATE_DIR/session_key.tmp" "$STATE_DIR/session_key" )
fi
chmod 600 "$STATE_DIR/session_key"
DASHBOARD_SESSION_KEY="$(cat "$STATE_DIR/session_key")"
# Hash du mot de passe : posé par infra/scripts/set-dashboard-password.sh ; absent => /login reste en 503.
DASHBOARD_PASSWORD_HASH="$(cat "$STATE_DIR/dashboard_password_hash" 2>/dev/null || true)"
export DASHBOARD_SESSION_KEY DASHBOARD_PASSWORD_HASH

COMPOSE_FILE="$HERE/compose.yaml"
PROBE="${VVA_PROBE:-1}"

compose() { # compose <tag> <args...>
  local tag="$1"; shift
  IMAGE_TAG="$tag" docker compose -p vva -f "$COMPOSE_FILE" --project-directory "$STATE_DIR" "$@"
}

start_slot() { # start_slot <slot> <tag> [pull] : démarre ce seul service, attend qu'il tourne
  compose "$2" up -d --pull "${3:-always}" --wait --wait-timeout 60 "dashboard_$1"
}

stop_slot() { # stop_slot <slot> : SIGTERM puis arrêt propre (stop_grace_period)
  compose "$VERSION" stop -t 30 "dashboard_$1" || true
}

drop_slot() { # drop_slot <slot> : arrête et supprime un slot raté
  compose "$VERSION" stop -t 10 "dashboard_$1" || true
  compose "$VERSION" rm -f "dashboard_$1" || true
}

# /healthz DANS le conteneur du slot (avant tout trafic) avec la version attendue.
slot_healthy() { # slot_healthy <slot> <version>
  local out
  for _ in $(seq 1 15); do
    out="$(compose "$2" exec -T "dashboard_$1" python -c \
      "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).read().decode())" \
      2>/dev/null || true)"
    case "$out" in *"\"version\":\"$2\""*) return 0 ;; esac
    sleep 2
  done
  return 1
}

# /healthz PUBLIC (via Traefik, round-robin ancien/nouveau) : la nouvelle version doit avoir répondu
# au moins 3 fois (cumul, non consécutives) sur 60 essais espacés de 0,5 s.
public_serves() { # public_serves <version>
  local out hits=0
  for _ in $(seq 1 60); do
    out="$(curl -fsS --max-time 5 "https://$DASHBOARD_HOST/healthz" 2>/dev/null || true)"
    case "$out" in *"\"version\":\"$1\""*) hits=$((hits + 1)) ;; esac
    [ "$hits" -ge 3 ] && return 0
    sleep 0.5
  done
  return 1
}

smoke() {
  local want="$1" out page
  for _ in $(seq 1 20); do
    out="$(curl -fsS --max-time 5 "https://$DASHBOARD_HOST/healthz" 2>/dev/null || true)"
    case "$out" in
      *"\"version\":\"$want\""*)
        page="$(curl -fsS --max-time 5 "https://$DASHBOARD_HOST/" 2>/dev/null || true)"
        case "$page" in *"Agents Dashboard"*) return 0 ;; esac ;;
    esac
    sleep 3
  done
  return 1
}

# Sondeur optionnel (VVA_PROBE=0 pour désactiver) : /healthz public toutes les 0,5 s, erreurs comptées.
PROBE_PID=""
PROBE_FILE=""
probe_start() {
  [ "$PROBE" = "1" ] || return 0
  PROBE_FILE="$(mktemp)"
  (
    while :; do
      curl -fsS -o /dev/null --max-time 2 "https://$DASHBOARD_HOST/healthz" 2>/dev/null || printf x >> "$PROBE_FILE"
      sleep 0.5
    done
  ) &
  PROBE_PID=$!
}
# shellcheck disable=SC2329 # appelée via trap EXIT
probe_stop() {
  [ -n "$PROBE_PID" ] || return 0
  kill "$PROBE_PID" 2>/dev/null || true
  wait "$PROBE_PID" 2>/dev/null || true
  PROBE_PID=""
  local n
  n="$(wc -c < "$PROBE_FILE" | tr -d ' ')"
  rm -f "$PROBE_FILE"
  if [ "$n" -gt 0 ]; then
    echo "::warning::Sondeur /healthz : $n erreur(s) pendant le déploiement"
  else
    echo "Sondeur /healthz : 0 erreur pendant le déploiement"
  fi
}
trap probe_stop EXIT

prev="$(cat "$STATE_DIR/last_good" 2>/dev/null || true)"
hint="$(cat "$STATE_DIR/slot" 2>/dev/null || true)" # indice seulement : la vérité = conteneurs en cours
# Slots réellement présents (docker compose ps -q ne liste que les conteneurs en cours).
running=""
for sl in blue green; do
  [ -n "$(compose "$VERSION" ps -q "dashboard_$sl" 2>/dev/null || true)" ] && running="$running $sl"
done
# Migration : ancien conteneur unique « dashboard » (projet vva) encore présent.
legacy="$(docker ps -a -q --filter label=com.docker.compose.project=vva \
  --filter label=com.docker.compose.service=dashboard 2>/dev/null || true)"
case "$running" in
  " blue") new=green ;;
  " green") new=blue ;;
  *) # aucun ou les deux : l'autre que l'indice
    if [ "$hint" = "blue" ]; then new=green; else new=blue; fi ;;
esac
other=blue; [ "$new" = "blue" ] && other=green
# Slot de rollback = l'ancien actif (autre que new) ; à défaut, l'autre slot.
rollback_slot="$other"
echo "Déploiement $VERSION dans le slot $new (précédente : ${prev:-aucune}, en cours :${running:- aucun}${legacy:+ + ancien conteneur unique})"

probe_start

if ! start_slot "$new" "$VERSION" || ! slot_healthy "$new" "$VERSION"; then
  echo "::error::Nouveau slot $new malsain pour $VERSION : aucune bascule, l'ancien continue de servir"
  drop_slot "$new"
  exit 1
fi
if ! public_serves "$VERSION"; then
  echo "::error::$VERSION n'a pas répondu 3 fois sur le /healthz public (3 réponses en 60 essais) : aucune bascule, l'ancien n'est pas arrêté"
  drop_slot "$new"
  exit 1
fi

# Arrêt systématique de tout ce qui n'est pas le nouveau slot.
if [ -n "$legacy" ]; then
  echo "Migration : arrêt de l'ancien conteneur unique"
  # shellcheck disable=SC2086 # plusieurs identifiants possibles, séparés par des retours à la ligne
  docker stop -t 30 $legacy >/dev/null || true
  # shellcheck disable=SC2086
  docker rm $legacy >/dev/null || true
fi
stop_slot "$other"

if smoke "$VERSION"; then
  echo "$new" > "$STATE_DIR/slot"
  echo "$VERSION" > "$STATE_DIR/last_good"
  docker image prune -f --filter "label=org.opencontainers.image.source" >/dev/null || true
  echo "OK : $VERSION en prod (slot $new)"
  exit 0
fi

echo "::error::Smoke test KO pour $VERSION"
if [ -z "$prev" ]; then
  echo "::error::Pas de version précédente connue : rollback impossible"
elif start_slot "$rollback_slot" "$prev" missing && slot_healthy "$rollback_slot" "$prev" && smoke "$prev"; then
  echo "$rollback_slot" > "$STATE_DIR/slot"
  drop_slot "$new"
  echo "Rollback OK"
else
  rm -f "$STATE_DIR/slot" # état incertain : on ne laisse pas un indice vers un slot cassé
  echo "::error::Rollback vers $prev KO : intervention manuelle requise (slots $new et $rollback_slot à vérifier)"
fi
exit 1
