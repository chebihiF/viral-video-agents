#!/bin/sh
# Passation automatique de session au seuil de contexte (#83). Voir docs/agents.md.
# check (hook UserPromptSubmit, JSON sur stdin) | post|relay --note-file F | latest.
exec python3 "$(dirname "$0")/../handoff.py" "$@"
