#!/usr/bin/env bash
# Crée $STATE_DIR/ingest_token (600) s'il n'existe pas, sans jamais l'afficher.
# Usage : ensure-token.sh <state_dir>
set -euo pipefail
f="$1/ingest_token"
if [ ! -s "$f" ]; then
  ( umask 077; openssl rand -hex 32 > "$f.tmp" && mv "$f.tmp" "$f" )
fi
chmod 600 "$f"
