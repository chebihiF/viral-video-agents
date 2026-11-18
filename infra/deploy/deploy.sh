#!/usr/bin/env bash
# Déploie le dashboard, vérifie /healthz en HTTPS, revient à la version précédente si échec.
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

up() {
  IMAGE_TAG="$1" docker compose -f "$HERE/compose.yaml" --project-directory "$STATE_DIR" \
    up -d --pull always --wait --wait-timeout 60
}

smoke() {
  local want="$1"
  for _ in $(seq 1 20); do
    if curl -fsS --max-time 5 "https://$DASHBOARD_HOST/healthz" | grep -q "\"version\":\"$want\""; then
      curl -fsS --max-time 5 "https://$DASHBOARD_HOST/" | grep -q "Agents Dashboard" && return 0
    fi
    sleep 3
  done
  return 1
}

prev="$(cat "$STATE_DIR/last_good" 2>/dev/null || true)"
echo "Déploiement $VERSION (précédente : ${prev:-aucune})"

if up "$VERSION" && smoke "$VERSION"; then
  echo "$VERSION" > "$STATE_DIR/last_good"
  docker image prune -f --filter "label=org.opencontainers.image.source" >/dev/null || true
  echo "OK : $VERSION en prod"
  exit 0
fi

echo "::error::Smoke test KO pour $VERSION"
if [ -n "$prev" ]; then
  echo "Rollback vers $prev"
  up "$prev" && smoke "$prev" && echo "Rollback OK" || echo "::error::Rollback KO"
fi
exit 1
