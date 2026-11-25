// Coquille : service worker, écran hors ligne, choix du thème. Aucun appel réseau.
// Envoi, progression et lecture : status.js, dictation.js, player.js (#214).
(function () {
  var accueil = document.getElementById("ecran-accueil");
  var erreur = document.getElementById("ecran-erreur");

  // Hors ligne : l'écran « pas de connexion » prend la place de l'écran courant (accueil, progression,
  // lecture…), qu'on note pour le rendre au retour du réseau. En ligne, on ne touche à rien tant que
  // l'écran « pas de connexion » n'est pas affiché : un événement « online » ne doit pas
  // réafficher l'accueil au milieu d'une progression.
  var masques = [];

  function visibles() {
    return Array.prototype.filter.call(document.querySelectorAll(".kid-screen"), function (s) {
      return s !== erreur && !s.hidden;
    });
  }

  function afficher() {
    var enLigne = navigator.onLine !== false;
    if (!enLigne) {
      if (!erreur.hidden) return;
      masques = visibles();
      masques.forEach(function (s) {
        var v = s.querySelector("video");
        if (v && v.pause) v.pause();
        s.hidden = true;
      });
      erreur.hidden = false;
      return;
    }
    if (erreur.hidden) return;
    erreur.hidden = true;
    var rendus = masques.filter(function (s) {
      return document.body.contains(s); // un écran fermé entre-temps ne revient pas
    });
    masques = [];
    if (!rendus.length) rendus = [accueil];
    rendus.forEach(function (s) {
      s.hidden = false;
    });
  }
  erreur.hidden = true;
  window.addEventListener("online", afficher);
  window.addEventListener("offline", afficher);
  afficher();

  erreur.querySelector(".kid-cta button").addEventListener("click", function () {
    location.reload();
  });

  // Saisie au clavier : la flèche Envoyer transmet le texte à MimoStatus (status.js), comme ✅ après la dictée.
  var TEXTE_MAX = 500; // contrat kid-job : au-delà, POST /api/jobs répond 400
  var idee = document.getElementById("idee");
  var fleche = idee.form.querySelector("button[type=submit]");

  function texteSaisi() {
    return String(idee.value || "").trim().slice(0, TEXTE_MAX);
  }

  function majFleche() {
    fleche.disabled = texteSaisi() === "";
  }

  idee.addEventListener("input", majFleche);
  idee.form.addEventListener("submit", function (e) {
    e.preventDefault();
    var texte = texteSaisi();
    if (!texte) return;
    idee.value = "";
    majFleche();
    window.MimoStatus.envoyer(texte);
  });
  majFleche();

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
