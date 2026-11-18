# Écran « Activité des agents »

Maquette validée par délégation du tech lead, à revoir par le parent : [maquette interactive](https://claude.ai/artifact/69kzaPBknVTePJuCFgDeRf) (5 états, clair/sombre). Épique #2, premier écran (#5).

## Objectif
Voir d'un coup d'œil qui travaille, sur quoi, combien ça coûte, et pouvoir mettre en pause.

## Structure HTML sémantique
```
<main>
  <header>  h1 « Activité des agents » · pastille connexion · boutons Mettre en pause / Reprendre
  <div role="alert|status">   bandeau erreur ou pause (si besoin)
  <section aria-label="Résumé">  4 tuiles : Agents actifs · Tokens du jour · Plafond du jour · Sprint en cours
  <section aria-labelledby>  h2 Agents : <table> (caption masquée, th scope="col")
  <aside aria-labelledby>    h2 Flux d'événements : <ol aria-live="polite">
```
Colonnes du tableau : Agent (+ modèle en mono) · Statut (pastille forme + libellé) · Tâche en cours · Tokens (jour, aligné à droite) · Dernière activité. Largeur >= 900 px : tableau et flux côte à côte ; en dessous, empilés, tableau en défilement horizontal.

## États
| État | Comportement |
|---|---|
| Temps réel | pastille « En direct » ; lignes et flux mis à jour ; flux en `aria-live="polite"` |
| Chargement | `aria-busy="true"`, lignes squelettes, valeurs « — » ; animation coupée si reduced-motion |
| Vide | message « Aucun agent n'a encore travaillé » + comment lancer un ticket |
| Pause | bandeau `role="status"` (warn) ; tous les agents « En pause » ; bouton « Reprendre les agents » |
| Erreur | bandeau `role="alert"` (danger) avec heure des dernières données et « Réessayer » ; lignes figées |

## Composants HTMX
- Tableau et flux : `hx-get` + `hx-trigger="load, every 5s"` ou extension SSE (`sse-connect`) ; swap `innerHTML` du `<tbody>` / de l'`<ol>`.
- Pause/Reprise : `hx-post`, `hx-swap="outerHTML"` sur le bandeau + en-tête ; bouton désactivé pendant la requête.
- Erreur réseau : `htmx:sendError` / `htmx:responseError` affiche le bandeau d'erreur ; « Réessayer » relance le `hx-get`.

## Accessibilité
Tout au clavier (boutons natifs), focus visible, état jamais par la couleur seule, cibles >= 36 px, chiffres tabulaires. Données de la maquette : exemples, aucune donnée réelle.
