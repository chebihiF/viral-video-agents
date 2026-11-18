# Runner auto-hébergé `vps-deploy` (VPS Hostinger)

Exécute uniquement le job `deploy` de `release.yml` (push sur `main`, environnement `production`
limité à `main`). Les PR, y compris de forks, ne tournent que sur les runners GitHub ; les PR de
contributeurs externes exigent une approbation manuelle.

## Installation (une fois, avec l'accord du propriétaire)
1. Utilisateur dédié sans mot de passe : `useradd -m -s /bin/bash gha-runner && usermod -aG docker gha-runner`
   (⚠️ le groupe docker équivaut à root sur l'hôte : le runner est réservé à ce dépôt).
2. Dossier d'état : `install -d -o gha-runner -g gha-runner /opt/vva` puis
   `/opt/vva/proxy.env` d'après `infra/deploy/proxy.env.example`.
3. En tant que `gha-runner`, télécharger le runner officiel (github.com/actions/runner/releases),
   vérifier le SHA-256, puis
   `./config.sh --url https://github.com/chebihiF/viral-video-agents --token <jeton d'inscription> --name vps --labels vps-deploy --unattended`
   (jeton : `gh api -X POST repos/chebihiF/viral-video-agents/actions/runners/registration-token -q .token`, valable 1 h).
4. Service : `sudo ./svc.sh install gha-runner && sudo ./svc.sh start`.

## Retour arrière
`sudo ./svc.sh uninstall`, `./config.sh remove --token <jeton>`, `docker compose -p vva down`,
`userdel -r gha-runner`, `rm -r /opt/vva`. n8n et Traefik ne sont pas modifiés.
