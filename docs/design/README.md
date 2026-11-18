# Design system du dashboard

Source des valeurs : `apps/dashboard/src/dashboard/static/tokens.css` (miroir lisible : [design system Claude Design](https://claude.ai/artifact/AmUybVqZu5w33VjnHuzYf2), lien privé).

## Contrat
- Les gabarits Jinja n'utilisent **que** les variables `--color-*`, `--space-*`, `--font-*`, `--radius-*` (plus `--font-size-*`, `--focus-ring`). Aucune couleur ni taille en dur.
- Thème : suit `prefers-color-scheme` ; `<html data-theme="light|dark">` force un thème.
- Polices : piles système (`--font-sans`, `--font-mono`), aucun CDN, aucun fichier distant.
- Focus visible 2 px (`:focus-visible`), `prefers-reduced-motion` neutralise animations et transitions.

## Couleurs
| Variable | Usage |
|---|---|
| `color-bg`, `color-surface`, `color-surface-2` | page ; cartes/tableaux ; en-têtes, survol |
| `color-text`, `color-text-muted` | texte principal ; secondaire |
| `color-border` / `color-border-subtle` | contrôles (>= 3:1) / filets décoratifs |
| `color-accent`, `color-on-accent`, `color-focus` | actions, texte sur accent, anneau de focus |
| `color-ok\|warn\|danger\|info` et `*-bg` | états ; le texte d'état se pose sur son `*-bg`, `surface` ou `bg` |

Règle : un état n'est jamais porté par la couleur seule (forme + libellé : ● Actif, ❚❚ En pause, ▲ Erreur, ○ Inactif).

## Échelles
Espacements 4 px : `space-1..7` = 4, 8, 12, 16, 24, 32, 48. Rayons : `radius-sm|md|lg|pill` = 4, 8, 12, 999. Texte : title 24, heading 18, body 15, small 13, label 12, data 14 (mono, chiffres tabulaires).

## Contrastes WCAG 2.2 AA (vérifiés par calcul, clair et sombre)
Texte et texte atténué >= 4,5:1 sur bg, surface, surface-2 ; états (ok, warn, danger, info) >= 4,5:1 sur leur `*-bg`, surface et bg ; texte sur accent >= 4,5:1 ; `color-border` et `color-focus` >= 3:1 sur bg et surface.

## Écrans
- [Activité des agents](activity.md)
- [Suivi du projet](progress.md)
