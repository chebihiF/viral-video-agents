# Écran « Suivi du projet » (`/progress`)

Maquette (artboard « Suivi du projet », variante unique, validée par délégation du tech lead, à revoir par le parent) : [maquette interactive](https://claude.ai/artifact/69kzaPBknVTePJuCFgDeRf). Même design system que [l'écran Activité](activity.md) : `tokens.css` uniquement.

## Objectif
Voir d'un coup d'œil où en est le projet (livré, en revue, en cours, prêt, backlog, en %) et ce que fait chaque agent, sans ouvrir GitHub.

## Structure HTML sémantique
```
<main>
  <header>  h1 « Suivi du projet » · sous-titre sprint
  <section aria-label="Avancement global">  anneau SVG (role="img" + aria-label) · « N livrés sur M tickets » · légende 5 statuts avec effectif
  <section aria-labelledby>  h2 « Par épique » : une ligne par épique (titre, « P % · N livrés / M », barre empilée) ; h2 « Par sprint » : idem ; encart « Colonnes et sprint du Project : bientôt disponible »
  <section aria-labelledby>  h2 « Agents au travail » : une carte par agent
  <section aria-labelledby>  h2 « Frise des livraisons » : <ol> chronologique
  <p> note discrète « Actualisé toutes les 60 s depuis GitHub »
```
Largeur >= 900 px : épiques/sprints à gauche, agents à droite ; en dessous, empilé.

## Composants
- **Barre empilée** (`role="img"`, `aria-label` : « <titre> : N livrés, … , P pour cent »). Segments à motif distinct, jamais la couleur seule : Livré = plein (`color-ok`), Revue = hachures (`color-info`), En cours = pointillé (`color-accent`), Prêt = contour, Backlog = vide. Le pourcentage et les effectifs sont aussi en texte.
- **Carte d'agent** : nom, modèle (mono), ticket, durée écoulée (mono), pastille pulsée si actif (« ○ Inactif » sinon), étapes Dév → Revue → QA → PR → Déploiement (✓ fait, ▶ en cours, libellé toujours visible).
- **Frise** : version, titre, PR, date, plus récent en premier (liste ordonnée) ; points `color-ok` reliés par un filet.

## Animations (toutes coupées par `prefers-reduced-motion`)
Remplissage des barres 0,9 s (échelonné), anneau, pulsation de la pastille d'agent (1,6 s). Reduced motion : états finaux affichés d'emblée ; l'information reste identique.

## Données et temps réel
`GET /api/v1/progress` (contrat du ticket #26). GitHub : API REST publique sans jeton, cache 60 s (décision D20) : issues, PR ouvertes = « en revue », labels, releases et tags pour la frise. Colonnes et sprint du Project : emplacement « bientôt disponible », branché plus tard par #9. Agents : événements SubagentStart/Stop et d'outils (#3, #4). Rafraîchissement HTMX : `hx-get` + `hx-trigger="every 60s"` sur les sections ; durée écoulée recalculée côté serveur.

## États
Chargement (squelettes, `aria-busy`), vide (« Aucun ticket »/« Aucun agent actif »), erreur GitHub (bandeau `role="alert"` + dernières données connues datées, « Réessayer »).

## Accessibilité
Contrastes de `tokens.css` (AA clair/sombre), état = forme + libellé, tout au clavier, focus visible, chiffres tabulaires.
