#!/usr/bin/env bash
# Tests de make-cert.sh (#160) : shellcheck, dry-run sans mkcert, refus d'écrire hors certs/, faux mkcert.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT
fail=0
# check <libellé> <commande...> : ok si la commande réussit.
check() { local label="$1"; shift; if "$@"; then echo "ok   $label"; else echo "FAIL $label" >&2; fail=1; fi; }
# refuse <libellé> <motif stderr> <commande...> : ok si la commande échoue avec ce message.
refuse() {
  local label="$1" pat="$2"; shift 2
  if "$@" >/dev/null 2>"$TMP/err"; then echo "FAIL $label (aurait dû échouer)" >&2; fail=1
  elif grep -q -- "$pat" "$TMP/err"; then echo "ok   $label"
  else echo "FAIL $label (message : $(cat "$TMP/err"))" >&2; fail=1; fi
}

if command -v shellcheck >/dev/null; then
  check shellcheck shellcheck "$SRC/make-cert.sh" "${BASH_SOURCE[0]}"
fi

# Copie du script dans un faux dépôt : certs/ s'y crée, jamais dans le vrai dépôt.
mkdir -p "$TMP/infra/kid-ui" "$TMP/bin"
cp "$SRC/make-cert.sh" "$TMP/infra/kid-ui/"
S="$TMP/infra/kid-ui/make-cert.sh"
C="$TMP/infra/kid-ui/certs"
# PATH sans mkcert réel : outils système de base + faux mkcert éventuel.
export PATH="$TMP/bin:/usr/bin:/bin"

out="$("$S" --dry-run 192.168.1.20 kid.local)"
check "dry-run sans mkcert" test "$out" = "dry-run : mkcert -cert-file $C/cert.pem -key-file $C/key.pem 192.168.1.20 kid.local"
check "dry-run n'écrit rien" test ! -e "$C"

refuse "mkcert absent : message clair" "mkcert introuvable" "$S" 192.168.1.20
refuse "refuse --out /tmp/x" "refus" "$S" --dry-run --out /tmp/x 192.168.1.20
refuse "refuse '..' dans --out" "refus" "$S" --dry-run --out "$C/../evil" 192.168.1.20
refuse "refuse un dossier voisin" "refus" "$S" --dry-run --out "$TMP/ailleurs" 192.168.1.20
refuse "exige un nom ou une IP" "au moins une IP" "$S" --dry-run
refuse "refuse un nom invalide" "invalide" "$S" --dry-run 'a;rm'
refuse "refuse une option inconnue" "option inconnue" "$S" --dry-run --x 192.168.1.20
check "sous-dossier de certs/ permis" "$S" --dry-run --out "$C/sous" 192.168.1.20 >/dev/null

# Liens symboliques : refus (dossier cible, fichier de sortie, certs/ lui-même).
mkdir -p "$C" "$TMP/dehors"
ln -s "$TMP/dehors" "$C/lien"
refuse "refuse --out lien symbolique" "lien symbolique" "$S" --dry-run --out "$C/lien" 192.168.1.20
refuse "refuse sous un lien symbolique" "lien symbolique" "$S" --dry-run --out "$C/lien/x" 192.168.1.20
mkdir -p "$C/ok"
ln -s "$TMP/dehors/vol.pem" "$C/ok/key.pem"
refuse "refuse key.pem lien symbolique" "lien symbolique" "$S" --dry-run --out "$C/ok" 192.168.1.20
rm -rf "$C"
ln -s "$TMP/dehors" "$C"
refuse "refuse certs/ lien symbolique" "lien symbolique" "$S" --dry-run 192.168.1.20
rm "$C"

# Faux mkcert : écrit les fichiers demandés.
cat >"$TMP/bin/mkcert" <<'F'
#!/usr/bin/env bash
if [ "${1:-}" = "-CAROOT" ]; then echo /nonexistent-caroot; exit 0; fi
while [ $# -gt 0 ]; do
  case "$1" in -cert-file) echo CERT >"$2"; shift ;; -key-file) echo KEY >"$2"; shift ;; *) ;; esac
  shift
done
F
chmod +x "$TMP/bin/mkcert"
"$S" 192.168.1.20 >/dev/null
check "cert.pem dans certs/" test -f "$C/cert.pem"
check "key.pem dans certs/" test -f "$C/key.pem"
check "certs/ en 700" test "$(find "$C" -maxdepth 0 -perm 700)" = "$C"
check "key.pem en 600" test "$(find "$C/key.pem" -perm 600)" = "$C/key.pem"
check "rien écrit hors certs/" test -z "$(find "$TMP" -name '*.pem' -not -path "$C/*")"

# Le vrai dépôt ignore certs/ (seulement si on est dans un dépôt git).
if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
  check "certs/ ignoré par git" git -C "$SRC" check-ignore -q "$SRC/certs/cert.pem"
fi
exit "$fail"
