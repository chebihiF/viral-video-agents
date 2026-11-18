#!/usr/bin/env bash
# Exécute une commande ; n'affiche sa sortie (40 dernières lignes) qu'en cas d'échec.
out="$("$@" 2>&1)"
code=$?
if [ "$code" -ne 0 ]; then
  printf '%s\n' "$out" | tail -40
fi
exit "$code"
