// Accueil : idées de départ et navigation clavier des radios. Aucun appel réseau, aucune donnée enregistrée.
(function () {
  // Les idées de départ sont des libellés fixes de l'appli : elles remplissent le champ, l'enfant l'envoie.
  var champ = document.getElementById("idee");
  document.querySelectorAll("[data-idee]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      champ.value = btn.getAttribute("data-idee");
      champ.dispatchEvent(new Event("input", { bubbles: true }));
      champ.focus();
    });
  });

  // Radios (role=radio) : une seule tabulation par groupe, flèches pour changer, Début/Fin pour les extrémités.
  var AVANT = { "ArrowRight": 1, "ArrowDown": 1, "ArrowLeft": -1, "ArrowUp": -1 };

  function majTabindex(radios) {
    var coche = radios.find(function (r) { return r.getAttribute("aria-checked") === "true"; }) || radios[0];
    radios.forEach(function (r) { r.setAttribute("tabindex", r === coche ? "0" : "-1"); });
  }

  function pas(touche, rtl) {
    var p = AVANT[touche];
    return rtl && (touche === "ArrowRight" || touche === "ArrowLeft") ? -p : p;
  }

  document.querySelectorAll("[role=radiogroup]").forEach(function (groupe) {
    var radios = Array.prototype.slice.call(groupe.querySelectorAll("[role=radio]"));
    var rtl = getComputedStyle(groupe).direction === "rtl";
    majTabindex(radios);
    radios.forEach(function (radio, i) {
      radio.addEventListener("click", function () { majTabindex(radios); });
      radio.addEventListener("keydown", function (e) {
        var cible;
        if (e.key === "Home") cible = 0;
        else if (e.key === "End") cible = radios.length - 1;
        else if (e.key in AVANT) cible = (i + pas(e.key, rtl) + radios.length) % radios.length;
        else return;
        e.preventDefault();
        radios[cible].click(); // même chemin que la souris : thème, aria-checked, stockage local
        radios[cible].focus();
      });
    });
  });
})();
