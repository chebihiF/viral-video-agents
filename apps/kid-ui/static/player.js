// Lecture de la vidéo terminée (#163) : lecteur, téléchargement, retour à l'accueil.
// Réagit à l'événement « mimo:video-prete » (status.js, #162) ; ne touche à aucun autre écran.
// La vidéo n'est jamais mise en cache (service worker inchangé) ; aucun texte de l'enfant ici.
(function (root) {
  "use strict";
  var UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

  // job_id -> adresses de lecture et de téléchargement ; null si ce n'est pas un UUID.
  function urls(jobId) {
    if (typeof jobId !== "string" || !UUID.test(jobId)) return null;
    var src = "/api/jobs/" + encodeURIComponent(jobId) + "/video";
    return { src: src, dl: src + "?download=1" };
  }

  function el(tag, cls, texte) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (texte) n.textContent = texte;
    return n;
  }

  function attrs(n, a) {
    Object.keys(a).forEach(function (k) {
      n.setAttribute(k, a[k]);
    });
    return n;
  }

  function montrer(cible) {
    document.querySelectorAll(".kid-screen").forEach(function (s) {
      s.hidden = s !== cible;
    });
  }

  function lecteur(src) {
    var v = attrs(el("video"), { controls: "", playsinline: "", preload: "metadata", src: src });
    v.setAttribute("aria-label", "Ta vidéo");
    v.style.cssText = "width:100%;max-height:70dvh;border-radius:var(--r-md);background:#000";
    return v;
  }

  function nouvelle() {
    var ecran = document.getElementById("ecran-lecture");
    if (ecran) {
      var v = ecran.querySelector("video");
      if (v) v.pause();
      ecran.remove(); // le lecteur est détruit : plus aucun flux ouvert
    }
    montrer(document.getElementById("ecran-accueil"));
  }

  function afficher(jobId) {
    var u = urls(jobId);
    if (!u) return;
    var vieux = document.getElementById("ecran-lecture");
    if (vieux) vieux.remove();
    var ecran = attrs(el("section", "kid-screen"), { id: "ecran-lecture", "aria-labelledby": "h-lecture" });
    var bar = el("header", "kid-bar");
    bar.appendChild(attrs(el("h1", null, "Ta vidéo"), { id: "h-lecture" }));
    var main = el("main", "kid-log");
    main.appendChild(lecteur(u.src));
    var cta = el("div", "kid-cta");
    var ligne = el("div", "kid-row");
    var dl = attrs(el("a", "kid-btn", "Télécharger"), { href: u.dl, download: jobId + ".mp4" });
    var neuve = attrs(el("button", "kid-btn primary lg", "Nouvelle vidéo"), { type: "button" });
    neuve.addEventListener("click", nouvelle);
    ligne.appendChild(dl);
    cta.append(ligne, neuve);
    ecran.append(bar, main, cta);
    document.body.appendChild(ecran);
    montrer(ecran);
  }

  var api = { urls: urls, afficher: afficher };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.MimoPlayer = api;
    document.addEventListener("mimo:video-prete", function (e) {
      afficher(e.detail && e.detail.job_id);
    });
  }
})(typeof window !== "undefined" ? window : this);
