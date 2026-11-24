// Thème de l'enfant : lu en local avant le rendu, jamais envoyé au serveur.
(function () {
  try {
    var t = localStorage.getItem("kid-theme");
    if (t === "nuit" || t === "soleil" || t === "menthe") {
      document.documentElement.setAttribute("data-theme", t);
    }
  } catch (e) {
    /* stockage indisponible : thème nuit par défaut */
  }
})();
