# Écran « Tokens » (`/tokens`) — spec alignée sur le réel (#129, écarts #118)

La maquette #118 est corrigée sur ces points ; le code fait foi.

## Quota
- Variables : `VVA_QUOTA_5H_TOKENS` (fenêtre de 5 h) et `VVA_QUOTA_WEEK_TOKENS` (semaine). Aucune autre (pas de `QUOTA_WEEKEND_TOKENS`).
- Fenêtres glissantes : 5 h = maintenant - 5 h ; semaine = 7 jours. Pas de remise à zéro, donc pas de « reset dans … » : aucune donnée de reset n'existe (hors périmètre).
- Week-end : part de la semaine tombée un samedi ou un dimanche (UTC), comparée au quota **semaine**. Pas de plafond propre, pas de marge propre.
- Quota non configuré : un seul bloc `.banner--info` `role="note"` en tête, qui nomme les variables manquantes (seulement celles-là). Les jauges concernées affichent les tokens bruts, jamais de pourcentage ni de barre.

## Niveaux
- Normal (moins de 70 %), Attention (70 % à moins de 90 %), Critique (à partir de 90 %). Badge = symbole + libellé + valeur ; la légende nomme les niveaux, jamais des couleurs.

## Tableaux
- Par agent / par ticket : fenêtre 5 h et 7 jours (pas 24 h), colonnes % masquées sans quota, pas de ligne Total ; tokens sans ticket sous « ticket inconnu ».
- Tendance : colonne « Variation vs la veille » incluse.
