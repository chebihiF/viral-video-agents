#!/usr/bin/env bash
# Certificat HTTPS local de l'appli enfant (mkcert) : sortie uniquement dans infra/kid-ui/certs/ (ignoré par git).
# Usage : infra/kid-ui/make-cert.sh [--dry-run] [--out DIR] <ip-ou-nom> [<ip-ou-nom> ...]
#   --dry-run  affiche la commande mkcert sans l'exécuter (mkcert n'a pas besoin d'être installé)
#   --out DIR  refusé sauf si DIR est infra/kid-ui/certs (ou un sous-dossier) : aucune écriture ailleurs
# La clé privée et la CA ne quittent jamais la machine ; ne commitez rien de certs/.
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CERTS="$HERE/certs"
dry=0
out="$CERTS"
names=()

die() { echo "make-cert : $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1 ;;
    --out) [ $# -ge 2 ] || die "--out attend un dossier"; out="$2"; shift ;;
    -*) die "option inconnue : $1 (usage : make-cert.sh [--dry-run] <ip-ou-nom>...)" ;;
    *) [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]*$ ]] || die "nom ou IP invalide : $1"; names+=("$1") ;;
  esac
  shift
done
[ "${#names[@]}" -gt 0 ] || die "donnez au moins une IP ou un nom (ex. 192.168.1.20 kid.local)"

# Écriture permise seulement sous infra/kid-ui/certs/ (comparaison lexicale, '..' interdit).
case "/$out/" in */../*) die "refus : '..' interdit dans --out ($out)" ;; esac
case "$out" in
  "$CERTS" | "$CERTS"/*) ;;
  *) die "refus d'écrire hors de $CERTS (reçu : $out)" ;;
esac
# Aucun lien symbolique entre certs/ (inclus) et la cible, ni sur les fichiers de sortie.
p="$out"
while :; do
  [ ! -L "$p" ] || die "refus : $p est un lien symbolique"
  [ "$p" != "$CERTS" ] || break
  p="$(dirname "$p")"
done
for f in cert.pem key.pem; do
  [ ! -L "$out/$f" ] || die "refus : $out/$f est un lien symbolique"
done
if [ -d "$out" ]; then
  real="$(cd "$out" && pwd -P)"
  case "$real" in "$CERTS" | "$CERTS"/*) ;; *) die "refus : $out sort de $CERTS (réel : $real)" ;; esac
fi

cmd=(mkcert -cert-file "$out/cert.pem" -key-file "$out/key.pem" "${names[@]}")
if [ "$dry" = 1 ]; then
  echo "dry-run : ${cmd[*]}"
  exit 0
fi

command -v mkcert >/dev/null || die "mkcert introuvable : voir infra/kid-ui/README.md (étape 1) ou l'alternative openssl"
mkdir -p "$out"
chmod 700 "$out"
"${cmd[@]}"
chmod 600 "$out/key.pem"
echo "ok : $out/cert.pem et $out/key.pem (jamais commités)."
echo "Pour le téléphone : envoyer SEULEMENT $(mkcert -CAROOT)/rootCA.pem, jamais rootCA-key.pem."
