# Refonte du dashboard — spec (#92, couvre #93 à #99)

Maquettes : https://claude.ai/artifact/SU58apG5uT2ihR9sFUS8fo (A recommandée : Main + Mobile ; B : Accueil-B ; états : Etats).
Fichiers : `project/tokens.css` (remplace `static/tokens.css`, rétrocompatible, jetons ajoutés), `project/components.css` (à copier en `static/components.css`, remplace `nav.css`).
Hypothèse : le texte des tickets #93-#99 n'a pas été lu (pas d'accès `gh`) ; les 5 pages existantes (Suivi, Activité, Tokens, Contrôles, Interactions) + Accueil sont couvertes.

## Structure HTML commune (gabarit de page)
```
<body><div class="app" [data-theme]>
  <a class="skip-link" href="#contenu">Aller au contenu</a>
  <nav class="sidenav" aria-label="Navigation principale"><ul><li><a class="navlink" [aria-current="page"]>…
  <main class="main" id="contenu">
    <div class="page-head"><h1> + .sub + .toolbar(.live, bouton pause)
    <section class="card" aria-labelledby=…>…
  <nav class="tabbar" aria-label="Navigation principale">   (mobile, voir ci-dessous)
```
Un seul `<h1>` par page ; `<h2>` par carte. Tableaux : `<caption>`, `<th scope="col">`, conteneur `.table-wrap` (défilement horizontal).

## Navigation (#99)
- >= 900 px : barre latérale (`.sidenav`), 6 entrées : Accueil, Suivi, Activité, Tokens, Contrôles, Interactions. `aria-current="page"` sur l'entrée active (fond `accent-bg` + texte accent).
- < 900 px : `.sidenav` masquée, barre d'onglets fixe en bas (`.tabbar`) : Accueil, Suivi, Activité, Tokens + « Plus » (`<details>`, feuille avec Contrôles, Interactions). Pas de JS. Cibles >= 44 px (56 px en barre).
- Une seule `<nav>` visible à la fois : dupliquer le gabarit `_nav.html` en deux listes, l'une masquée par CSS (`display:none` la retire de l'arbre d'accessibilité).
- Lien d'évitement « Aller au contenu » en premier élément focalisable.

## Accueil (#93)
Ordre : (1) carte « À faire par toi » (`.card--attention`, masquée si vide) ; (2) 4 KPI (`.grid-kpi` : tickets du sprint avec `.bar`, agents actifs, tokens du jour, erreurs 24 h) ; (3) deux colonnes : agents en ce moment, flux d'activité (`ol.rows.timeline`, plus récent en haut).
Temps réel : conteneur `hx-ext="sse"` ou polling HTMX existant ; chaque carte est un fragment rafraîchissable (`hx-get`, `hx-trigger="load, every 5s"`, `hx-swap="innerHTML"`), `aria-live` absent des listes (trop bavard) ; seul le bandeau d'état porte `role="status"`.
Variante B (non recommandée) : navigation haute + tableau unique ; plus compacte mais perd les KPI d'un coup d'œil.

## Pages #94-#97
Même gabarit : `.page-head` (titre + `.live`), formulaire de filtres `role="search"` (`label.sel` + `select`/`input`, `.btn`), contenu en `.card` ou `.table-wrap`. Filtres envoyés par `hx-get` + `hx-target` vers le fragment, `hx-push-url="true"`.

## États (#98)
| État | Composant | Règle |
|---|---|---|
| Vide | `.state` : icône, `<h2>`, 1 phrase, 1 action | dire quoi faire ensuite |
| Chargement | `.skeleton` dans conteneur `aria-busy="true"` | statique si reduced-motion |
| Erreur / flux coupé | `.banner--danger role="alert"` + « Réessayer » | garder les dernières données, horodatées |
| Pause | `.banner--warn role="status"` + « Reprendre » | le bouton Pause de l'en-tête bascule |
| Nouveautés | `.banner--info role="status"` « N nouveaux événements » | |
| Temps réel actif | `.live` (point pulsant + texte « En direct ») | point fixe en reduced-motion |

États toujours = forme + libellé : ● Actif, ❚❚ En pause, ▲ Erreur/Bloqué, ○ Inactif, ✓ Livré.

## Accessibilité et sobriété
Contrastes AA vérifiés pour les paires ajoutées (`accent` sur `accent-bg` : 5,8:1 clair et sombre). Focus 2 px visible partout. Clavier : ordre DOM = ordre visuel. `prefers-reduced-motion` : animations coupées (tokens.css). Aucune police ni lib externe. Données des maquettes fictives, aucune donnée d'enfant.

## Version enfant (plus tard, hors périmètre)
Réutiliser `tokens.css` avec une autre feuille de composants : `--size-touch` 72 px, `dir="rtl"`, icônes + voix, un seul bouton par écran.
