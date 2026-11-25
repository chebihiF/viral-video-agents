# Spec galerie parent (ticket #151)

Page statique `galerie.html` pour le parent, ouverte en local (`file://`) : un seul fichier, CSS inline, **aucun JS, aucune URL http(s)**, aucune police distante. Aucun texte d'enfant (idée, transcription) : seulement `run_id`, état, durée. Sobre : ce n'est pas l'UI Mimo (pas de mascotte ni d'autocollants), mais mêmes principes (icône + mot, focus visible, reduced-motion). Direction Mimo validée par le parent ; le visuel de la galerie reste volontairement neutre. Non testé avec le parent.

## Structure
```html
<!doctype html><html lang="fr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Galerie des vidéos</title>
<style>/* voir CSS */</style>
<body><header><h1>Galerie des vidéos</h1><p class="meta">3 runs</p></header>
<main><ul class="runs">
  <li><article class="run" aria-labelledby="r-ab12">
    <h2 id="r-ab12"><code>ab12cd34</code></h2>
    <p class="state ok"><span aria-hidden="true">✔</span> Terminé</p>
    <p class="dur"><time datetime="PT1M42S">1 min 42 s</time></p>
    <video controls preload="none" src="runs/ab12cd34/final.mp4"></video>
  </article></li></ul></main></body></html>
```
- États (icône + mot, jamais la couleur seule) : `✔ Terminé` (ok), `● En cours` (info), `▲ Échec` (danger), `○ En attente` (neutre). Icônes `aria-hidden`.
- `<video>` **seulement si Terminé** (`src` relatif, `controls`, `preload="none"`, pas d'`autoplay`). Autres états : pas de lecteur ; durée affichée "—" si inconnue (jamais de faux chiffre).
- Durée en minutes + secondes (`1 min 42 s`), `<time datetime>` ISO 8601.
- Tout `run_id` est échappé HTML par le générateur (valeur de fichier, pas de confiance).
- Tri : plus récent d'abord. Pas de pagination en v1.

## État vide
Aucun run : `<p class="empty" role="status">Aucune vidéo pour l'instant.</p>` à la place du `<ul>`, `h1` conservé, compteur "0 run".

## CSS inline minimal
```css
:root{--color-bg:#f7f7f9;--color-surface:#fff;--color-text:#1b1b21;--color-muted:#4a4a57;
--color-border:#767684;--color-ok:#0b6b2e;--color-info:#0b5394;--color-danger:#a31515;
--color-focus:#1b1b21;--space-2:8px;--space-3:12px;--space-4:16px;--radius:8px;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--color-bg:#14141a;--color-surface:#1e1e27;
--color-text:#ececf3;--color-muted:#b4b4c4;--color-border:#8a8a9c;--color-ok:#6fd48f;
--color-info:#7db8ff;--color-danger:#ff8f8f;--color-focus:#ffd84d}}
body{margin:0;padding:var(--space-4);background:var(--color-bg);color:var(--color-text);
font:15px/1.5 system-ui,sans-serif}
.runs{list-style:none;margin:0;padding:0;display:grid;gap:var(--space-4);
grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr))}
.run{background:var(--color-surface);border:1px solid var(--color-border);
border-radius:var(--radius);padding:var(--space-4)}
.run video{width:100%;max-width:100%;margin-top:var(--space-3);border-radius:var(--radius)}
code{font:14px ui-monospace,monospace}.meta,.dur{color:var(--color-muted)}
.state{font-weight:600}.ok{color:var(--color-ok)}.info{color:var(--color-info)}.bad{color:var(--color-danger)}
:focus-visible{outline:3px solid var(--color-focus);outline-offset:3px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
```
Pas d'animation prévue en v1 ; la règle reduced-motion est une garde. Contrastes à revérifier avec un outil (axe/Lighthouse) avant prod : valeurs calculées à la main (texte >= 4,5:1, contour et focus >= 3:1).

## Accessibilité
Un `h1`, un `h2` par carte (le `run_id`), liste sémantique, lecteur natif au clavier, cibles >= 44 px (contrôles natifs). Mobile d'abord (une colonne sous 300 px, 360 px OK).

## Questions pour le parent
- Afficher aussi la date de création du run (utile au tri) ?
