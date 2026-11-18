#!/usr/bin/env bash
# Définit le mot de passe du dashboard (à lancer par le parent, sur le Mac).
# Le mot de passe est lu 2 fois sans écho, haché localement (PBKDF2-SHA256, même format que
# dashboard.auth.hash_password) puis seul le hash part par SSH vers $VVA_REMOTE_FILE (600).
# Le mot de passe n'est ni en argument, ni dans un fichier, ni dans un log.
# Le conteneur le prend en compte au prochain déploiement du pipeline (prochain merge sur main).
#
# Configuration (surchargeable, utile pour les tests) :
#   VVA_SSH          commande ssh                 (défaut : ssh)
#   VVA_SSH_KEY      clé privée                   (défaut : ~/.ssh/vva_vps_ed25519)
#   VVA_SSH_TARGET   utilisateur@hôte             (défaut : root@srv1241648.hstgr.cloud)
#   VVA_REMOTE_FILE  fichier du hash sur le VPS   (défaut : /opt/vva/dashboard_password_hash)
#   VVA_REMOTE_OWNER propriétaire du fichier      (défaut : gha-runner)
set -euo pipefail

SSH="${VVA_SSH:-ssh}"
KEY="${VVA_SSH_KEY:-$HOME/.ssh/vva_vps_ed25519}"
TARGET="${VVA_SSH_TARGET:-root@srv1241648.hstgr.cloud}"
REMOTE_FILE="${VVA_REMOTE_FILE:-/opt/vva/dashboard_password_hash}"
OWNER="${VVA_REMOTE_OWNER:-gha-runner}"
MIN_LEN=10
ITERATIONS=600000 # = DEFAULT_ITERATIONS de dashboard.auth

# Lit stdin (mot de passe), écrit le hash sur stdout. Aucun secret en argument.
hash_stdin() {
  python3 -I -c '
import hashlib, secrets, sys
password = sys.stdin.buffer.read()
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", password, salt, int(sys.argv[1]))
print(f"pbkdf2_sha256:{sys.argv[1]}:{salt.hex()}:{digest.hex()}")
' "$ITERATIONS"
}

printf 'Nouveau mot de passe du dashboard : ' >&2
IFS= read -rs pw1
printf '\nConfirmez : ' >&2
IFS= read -rs pw2
printf '\n' >&2
if [ "$pw1" != "$pw2" ]; then
  echo "Les deux saisies diffèrent : rien n'a été modifié." >&2
  exit 1
fi
if [ "${#pw1}" -lt "$MIN_LEN" ]; then
  echo "Mot de passe trop court (minimum $MIN_LEN caractères) : rien n'a été modifié." >&2
  exit 1
fi

hash="$(printf '%s' "$pw1" | hash_stdin)"
unset pw1 pw2

# Écriture atomique côté VPS : fichier temporaire 600, puis renommage.
remote="$(printf 'umask 077; cat > %q.tmp && chown %q %q.tmp && chmod 600 %q.tmp && mv %q.tmp %q' \
  "$REMOTE_FILE" "$OWNER" "$REMOTE_FILE" "$REMOTE_FILE" "$REMOTE_FILE" "$REMOTE_FILE")"
printf '%s\n' "$hash" | $SSH -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$TARGET" "$remote"

echo "Hash enregistré. Il sera actif au prochain déploiement du pipeline (prochain merge sur main)." >&2
