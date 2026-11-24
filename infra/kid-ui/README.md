# HTTPS local pour l'appli enfant (#160)

Le micro (`getUserMedia`) et l'installation PWA exigent HTTPS. Le serveur tourne sur le réseau de la maison (LAN), jamais sur Internet : aucun port n'est ouvert sur la box, aucun service payant. Outil : [mkcert](https://github.com/FiloSottile/mkcert) (BSD-3, gratuit).

Règles : la clé privée (`key.pem`) et la CA locale (`rootCA-key.pem`) ne sont **jamais** commitées ni envoyées hors de la machine. Seul `rootCA.pem` (la partie publique) va sur le téléphone. `infra/kid-ui/certs/` est ignoré par git ; le script refuse d'écrire ailleurs.

## 1. Sur l'ordinateur serveur (une fois)

1. Repérer son IP LAN, par exemple `192.168.1.20` (macOS : Réglages > Wi-Fi > Détails ; Linux : `ip -4 addr`). Idéalement la fixer dans la box (bail DHCP statique) : le certificat est lié à cette IP.
2. Installer mkcert : `brew install mkcert` (macOS), `sudo apt install mkcert` (Debian/Ubuntu récent) ou le binaire des releases GitHub.
3. Créer l'autorité locale : `mkcert -install` (ajoute la CA au magasin de confiance de l'ordinateur ; demande le mot de passe système).
4. Générer le certificat pour l'IP (et un nom si voulu) :
   ```sh
   infra/kid-ui/make-cert.sh 192.168.1.20 kid.local
   ```
   Vérifier avant : `infra/kid-ui/make-cert.sh --dry-run 192.168.1.20` affiche la commande sans rien écrire. Résultat : `infra/kid-ui/certs/cert.pem` et `key.pem` (droits 600).
5. Trouver le certificat racine à donner au téléphone : `mkcert -CAROOT` affiche un dossier ; le fichier à envoyer est **`rootCA.pem`** (jamais `rootCA-key.pem`).
6. Lancer le serveur sur l'interface LAN :
   ```sh
   python3 apps/kid-ui/server.py --host 192.168.1.20 --tls-cert infra/kid-ui/certs/cert.pem --tls-key infra/kid-ui/certs/key.pem
   ```
   Sans `--host`, il n'écoute que `127.0.0.1` (le téléphone ne peut alors pas l'atteindre). Sans certificat, il refuse de démarrer, sauf `--insecure-dev` (essai local, micro et PWA indisponibles). Autoriser le port 8090 dans le pare-feu de l'ordinateur pour le LAN seulement.

## 2. Installer la CA sur le téléphone

Transférer `rootCA.pem` par câble USB, AirDrop, ou un mail à soi-même (c'est un fichier public ; ne jamais envoyer `rootCA-key.pem`).

### Android
1. Copier `rootCA.pem` dans le dossier Téléchargements du téléphone.
2. Ouvrir Paramètres > Sécurité (ou « Sécurité et confidentialité ») > Autres paramètres de sécurité > Chiffrement et identifiants > Installer un certificat > **Certificat CA**.
3. Un avertissement « votre réseau peut être surveillé » s'affiche : c'est normal pour une CA ajoutée à la main ; confirmer « Installer quand même » et choisir `rootCA.pem`. Le téléphone demande le code de verrouillage.
4. Vérifier : Chiffrement et identifiants > Certificats de confiance > onglet Utilisateur : la CA « mkcert … » apparaît.
5. Ouvrir Chrome sur `https://192.168.1.20:8090/` : le cadenas est fermé, pas d'alerte. Menu > « Installer l'application » pour la PWA.

### iOS (iPhone/iPad)
1. Ouvrir `rootCA.pem` (AirDrop, Fichiers ou pièce jointe) : « Profil téléchargé ».
2. Réglages > Général > VPN et gestion de l'appareil > profil « mkcert … » > **Installer** (code requis) > Installer.
3. **Étape facile à oublier** : Réglages > Général > Informations > Réglages des certificats > activer **Confiance totale** pour « mkcert … ».
4. Ouvrir Safari sur `https://192.168.1.20:8090/` : pas d'alerte. Partager > « Sur l'écran d'accueil » pour la PWA.

## 3. Vérifier le micro (contrôle manuel)

Sur le téléphone, ouvrir l'appli : pas d'alerte de sécurité, et le navigateur propose d'autoriser le micro (Android : cadenas > Autorisations > Micro ; iOS : Réglages > Safari > Micro). Si une alerte apparaît, la CA n'est pas installée ou pas « de confiance », ou l'adresse tapée n'est pas celle du certificat (régénérer avec la bonne IP).

## Alternative sans mkcert : certificat auto-signé openssl

Même installation de CA sur le téléphone (le certificat lui-même sert de CA ; `subjectAltName` obligatoire, sinon les navigateurs le rejettent) :

```sh
mkdir -p infra/kid-ui/certs && chmod 700 infra/kid-ui/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=kid-ui" \
  -addext "subjectAltName=IP:192.168.1.20,DNS:kid.local" \
  -keyout infra/kid-ui/certs/key.pem -out infra/kid-ui/certs/cert.pem
chmod 600 infra/kid-ui/certs/key.pem
```

Envoyer `cert.pem` au téléphone et suivre les étapes Android/iOS ci-dessus (à la place de `rootCA.pem`). Sur Android récent, certains navigateurs refusent un certificat feuille installé comme CA : préférer mkcert.

## Renouvellement et nettoyage

Le certificat mkcert dure environ 2 ans 3 mois ; pas de renouvellement automatique (hors périmètre) : relancer `make-cert.sh`. Pour retirer la confiance : `mkcert -uninstall` sur l'ordinateur, et supprimer le certificat dans les réglages du téléphone.

## Tests

`bash infra/kid-ui/tests/test_cert_script.sh` (shellcheck, dry-run sans mkcert, refus d'écrire hors `certs/`, faux mkcert), lancé aussi par `make check` via `test_cert_script.py`. Le démarrage HTTPS est testé dans `apps/kid-ui/tests/test_tls.py` avec un certificat jetable généré dans un dossier temporaire.
