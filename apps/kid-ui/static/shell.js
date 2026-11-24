// Coquille : service worker, écran hors ligne, choix du thème. Aucun appel réseau.
(function () {
  var accueil = document.getElementById("ecran-accueil");
  var erreur = document.getElementById("ecran-erreur");

  function afficher() {
    var enLigne = navigator.onLine !== false;
    accueil.hidden = !enLigne;
    erreur.hidden = enLigne;
  }
  window.addEventListener("online", afficher);
  window.addEventListener("offline", afficher);
  afficher();

  erreur.querySelector(".kid-cta button").addEventListener("click", function () {
    location.reload();
  });

  document.getElementById("idee").form.addEventListener("submit", function (e) {
    e.preventDefault(); // la dictée arrive avec un autre ticket
  });

  var radios = document.querySelectorAll(".kid-theme [role=radio]");
  var noms = ["nuit", "soleil", "menthe"];
  radios.forEach(function (btn, i) {
    var actif = (document.documentElement.getAttribute("data-theme") || "nuit") === noms[i];
    btn.setAttribute("aria-checked", String(actif));
    btn.addEventListener("click", function () {
      document.documentElement.setAttribute("data-theme", noms[i]);
      radios.forEach(function (b, j) {
        b.setAttribute("aria-checked", String(j === i));
      });
      try {
        localStorage.setItem("kid-theme", noms[i]);
      } catch (e) {
        /* stockage indisponible : le choix vaut pour cette visite */
      }
    });
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register('/sw.js', { scope: "/" }).catch(function () {
      /* sans service worker, l'appli reste utilisable en ligne */
    });
  }
})();
