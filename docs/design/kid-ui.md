# Spec UI enfant "Mimo" (ticket #36, pour #161 #162 #163 #165)

Cible : téléphone 360 px, enfant de 8-10 ans, darija/français, RTL prévu. Direction "autocollants" : couleurs franches, contour 2 px, ombre dure (`--edge`), mascotte Mimo (chat orange SVG inline). Non testé avec des enfants. Maquette validée par le parent (lien privé, hors dépôt).

Fichiers livrés (ne pas réécrire) : `apps/kid-ui/static/tokens-enfant.css` (tokens + composants + mouvement), `shell.css` (`[hidden]`), `index.html` (coquille accueil + erreur). Toute nouvelle classe va dans un fichier à part ou dans `tokens-enfant.css` après accord du design.

## Principes
- Un écran = `section.kid-screen` (hauteur `--screen-h`, défaut `100dvh`), un seul visible (`hidden` sur les autres). Titre `h1` dans `header.kid-bar`, contenu dans `main.kid-log`, action principale dans `.kid-cta` (hors du défilement), saisie dans `form.kid-composer`.
- Texte de l'enfant : toujours inséré échappé (`textContent`), jamais comme libellé de bouton ni `innerHTML`. Libellés de l'appli fixes.
- Aucun état porté par la couleur seule : icône + mot. Pas de texte long : phrase de 6-8 mots max par état.
- Cibles >= 48 px (60 px : Créer la vidéo, Lire, Réessayer). Texte >= 16 px (étiquettes 14 px).

## Thèmes
`data-theme="nuit|soleil|menthe"` sur `<html>` (ou sur `.kid-screen`). Sans attribut : `prefers-color-scheme` (sombre = nuit, clair = soleil). Choix enfant dans `.kid-theme[role=radiogroup]`, stocké en `localStorage` clé `kid-theme` (try/catch), appliqué par `/theme.js` avant le rendu. Rien envoyé au serveur. Flèches du clavier = déplacer le choix (roving tabindex), Espace/Entrée = valider. `<meta name=theme-color>` suit le thème (nuit `#120E2B`).

## Écrans et états
| Écran | Structure | Notes |
|---|---|---|
| Accueil | `.hero` (Mimo.xl.bob + h2), `ul.starters` de `a.kid-btn` (3 idées fixes), `.kid-theme`, composer | existe dans `index.html` |
| Écoute | `.kid-listen[role=group][aria-label]` : `.live` (rec-dot + "Je t'écoute"), `.stage` (Mimo + 3 `i`), `.kid-bars`, `.heard[aria-live=polite]`, `.kid-row` (Annuler / "J'ai fini" primary) | remplace log + composer |
| Réponse | `.msg.me` puis `.msg.bot` (svg.mimo + `.bub` + `[role=radiogroup].variants`), `.kid-cta` bouton `lg primary` | "Créer la vidéo" `disabled` tant qu'aucune version cochée (`aria-checked=true`) |
| Progression | voir ci-dessous | #165 |
| Vidéo prête | `.confetti[aria-hidden]`, `.poster` (+ `.kid-chip.dur`), h2 "Ta vidéo est prête !", `a.kid-btn.primary.lg` "Lire" | confettis une fois |
| Mes vidéos | `ul.grid` > `li` > `a.card` (`.pic` + `.kid-chip`, `b` titre, `.d` date) ; `.card.wait` = en cours | `.kid-foot` = retour |
| Hors maison | `.home-scene` (Mimo endormi + `.zzz`), `.note.warn[role=status]` | voir ci-dessous |
| Erreur | `.scene` (Mimo étourdi `.dizzy`), `.note.bad[role=alert]`, `.kid-cta` Réessayer `lg primary` | existe dans `index.html` |

### Progression (#165, durée restante)
```html
<section class="kid-screen" aria-labelledby="h-prog">
  <header class="kid-bar"><h1 id="h-prog">Ta vidéo arrive</h1></header>
  <main class="kid-log" hx-get="/video/{id}/etat" hx-trigger="every 3s" hx-swap="innerHTML">
    <div class="scene"><svg class="mimo big bob">…</svg></div>
    <div class="kid-seg" role="progressbar" aria-label="Avancement"
         aria-valuemin="0" aria-valuemax="4" aria-valuenow="2"
         aria-valuetext="Étape 2 sur 4, environ 2 minutes restantes"><i class="on"></i><i class="on"></i><i></i><i></i></div>
    <ol class="film"><li class="done">…</li><li class="now" aria-current="step">…</li><li>…</li><li>…</li></ol>
    <p class="say"><svg class="kid-spin" aria-hidden="true">…</svg>Je dessine…</p>
    <p class="kid-chip info">Encore environ 2 min</p>
  </main>
</section>
```
- Durée restante : `kid-chip info` + icône horloge, texte arrondi à la minute ("Encore environ 2 min"), jamais de secondes. Si < 1 min : "Presque fini". Si inconnue : chip omise (pas de faux chiffre).
- "Bientôt prête" (dernière étape, ou durée < 1 min) : chip `kid-chip ok` "Bientôt prête" + icône étoile, remplace la chip durée ; le Mimo reste `bob`. Annoncé une seule fois via `role=status` (pas à chaque tick de 3 s : ne remplacer que si le texte change, sinon `hx-swap=none`).
- Étape terminée : `.film .done` (coche) ; courante : `.now` + `aria-current=step` ; texte `.st` ("fait", "en cours", "bientôt"). L'état final arrête le polling (réponse sans `hx-trigger`) et passe à Vidéo prête.
- Erreur serveur pendant l'attente : écran Erreur (le polling reprend sur Réessayer).

### Hors maison
`.note.warn[role=status]` : h2 "Mimo dort un peu" (court), `p` : **« Je te génère la vidéo une fois à la maison. »** + `kid-chip warn` "Hors maison". L'idée est gardée localement et envoyée au retour (message `.kid-sys` "Idée gardée"). Pas de bouton Créer actif (`disabled`, tirets). Composer `aria-disabled=true` (pointillé).

## Contrastes
Calculés à la main dans la maquette : texte >= 4,5:1, contours/focus >= 3:1. **À revérifier avec un outil (axe/Lighthouse, WCAG 2.2 AA) avant prod**, notamment : `--ink-2` sur `--surface-2` (3 thèmes), `--on-pop` sur `--pop`, chips `warn/ok/info/bad` sur leur fond, `--line` nuit (#7467D1) sur `--bg` (contour d'un composant : >= 3:1 requis). Focus : `outline: 3px solid var(--focus)`, décalage 3 px (jaune en nuit, encre en clair). Bouton `disabled` : exempté du contraste mais lisible (tirets + texte).

## Mouvement
Tout est coupé par `prefers-reduced-motion: reduce` (animation/transition `none`). Durées : entrée message 350 ms ; boucles lentes (bob 3,2 s, ring 2,4 s) ; polling sans clignotement. Rien de plus de 3 flashs/s. Sans animation l'état final reste lisible (message visible, anneaux masqués).

## Mapping tokens existants
Fond `--bg`/`--bg-dot` ; cartes `--surface`, secondaire `--surface-2` ; texte `--ink`, discret `--ink-2` ; contour `--line`, ombre `--edge` ; action `--brand`/`--on-brand` ; accent `--pop`/`--on-pop` ; `--sky` pour variante 2 ; états `--ok/--warn/--bad/--info` (+ `-bg`) via `.kid-chip.*` et `.note.*` ; focus `--focus` ; rayons `--r-sm/md/lg` ; cibles `--touch` 48, `--touch-lg` 60 ; relief `--press` 4 px ; polices `--font-display`, `--font-body`.
Aucun nouveau token nécessaire pour les états ci-dessus : réutiliser `.kid-chip`, `.say`, `.note`, `.kid-seg`, `.film`.

## Écarts maquette / tokens livrés (à traiter par les devs ou à arbitrer)
1. ~~**Polices**~~ **Traité #222** (woff2 latins locaux, précachés) : aucun `@font-face` dans `tokens-enfant.css`. Ajouter woff2 locaux (Bricolage Grotesque, Figtree, OFL) avec `font-display: swap` ; sans eux, repli système (acceptable hors ligne).
2. **Arabe** : pas de police arabe. Ajouter Readex Pro ou Tajawal (OFL, locale) dans les deux piles avant la darija en script arabe.
3. **Traité #222** (liseré clair `--mimo-halo` posé en thème nuit selon la spec, à valider par capture du parent) — **Mimo en couleurs figées** (`#FF8A1F`, `#1B1340` dans le SVG) : OK, mais le contour sombre `#1B1340` disparaît sur fond nuit `#120E2B` ; la tête reste lisible grâce à l'orange, vérifier visuellement ; sinon ajouter un liseré clair.
4. **Pastilles de thème** `.t-nuit/.t-soleil/.t-menthe` et `.poster` (`#1F2A5C`) utilisent des hex en dur (volontaire : aperçu du thème), à ne pas "tokeniser".
5. **Confettis** : sous reduced-motion l'animation est coupée mais les `i` restent affichés. Ajouter `@media (prefers-reduced-motion: reduce) { .confetti { display: none; } }`.
6. **RTL** : `.poster .play` (`margin: 0 0 0 -28px`), `.kid-composer textarea` et `.kid-theme` (padding) sont en propriétés physiques : passer en `margin-inline-start` / `padding-inline-*` avant le RTL. Les flèches portent `.dir`.
7. **`.kid-icon-btn`** 46 px (< 48 de `--touch`) : conforme WCAG 2.2 (>= 24 px) mais incohérent ; le passer à `var(--touch)`.
8. **Traité #222** (écran erreur `hidden` au départ, `<title>` fixe, idées de départ en boutons) — **Coquille** : `index.html` n'a pas de `hidden` sur l'écran erreur ni de routage ; `shell.js` gère la bascule (`#hash` ou état), et le titre `<title>` doit rester "Mimo". Les liens `#idee-n` sont des ancres provisoires.
9. **Traité #222** (`.kid-chip.dur`, `.poster .dur` stylé) — **Manque** : pas de classe pour la chip durée/"Bientôt prête" (utiliser `.kid-chip.info` / `.ok`) ni pour `.dur` du poster (positionné, non stylé hors chip).
10. **Traité #222** (`accueil.js`) — Radios (`role=radio`) : la navigation aux flèches n'est pas fournie par le CSS, à coder (#161).

## Questions ouvertes pour le parent
- Faut-il afficher la durée restante à l'enfant (peut décevoir si estimation fausse) ou seulement "Bientôt prête" ?
- Mimo parle-t-il (voix synthétique) en darija dès la v1 ? Impacte le choix de police arabe et les libellés.
