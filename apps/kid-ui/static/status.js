// Suivi d'une vidéo (#162) : envoi de la dictée à /api/jobs, puis statut interrogé toutes les 2 s.
// Écrans de la maquette (#36) : Envoi, Vidéo en cours, Hors maison, Erreur (+ « prête », lecture en #163).
// La dictée n'est ni affichée, ni stockée, ni tracée : elle reste en mémoire pour « Réessayer ».
// Point d'entrée : MimoStatus.envoyer(texte), appelé par l'écran de validation (#161).
(function (root) {
  "use strict";
  var POLL_MS = 2000;
  var MSG_MAISON = "Je te génère la vidéo une fois à la maison.";
  var SVG = "http://www.w3.org/2000/svg";
  // Ordre réel du parcours (orchestrator.pipeline) : la voix passe avant les images.
  var ETAPES = ["storyboard", "voix", "images", "montage"];
  var NOMS = { storyboard: "Histoire", voix: "Voix", images: "Images", montage: "Montage" };
  var PHRASES = {
    storyboard: "J'écris l'histoire…",
    voix: "Je prépare la voix…",
    images: "Je dessine…",
    montage: "Je monte la vidéo…"
  };

  // Réponse de l'API (contracts/kid-job.schema.json) -> écran à afficher. Pure, testée sous node.
  function vue(rep) {
    if (rep === null || rep === undefined) return { ecran: "envoi" };
    if (rep.status === "deferred" || rep.state === "deferred") return { ecran: "hors-maison" };
    if (typeof rep.job_id === "string") return { ecran: "en-cours", etape: 0 };
    if (rep.state === "queued" || rep.state === "running") {
      var i = ETAPES.indexOf(rep.step);
      return { ecran: "en-cours", etape: i < 0 ? 0 : i };
    }
    if (rep.state === "done") return { ecran: "prete" };
    return { ecran: "erreur", bloque: rep.blocked === true }; // bloquée : réessayer est inutile
  }

  // --- Rendu : tout passe par textContent, aucun texte de l'enfant n'est inséré ---
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

  function mimo(source, classes) {
    var m = document.querySelector(source);
    if (!m) return el("span");
    var copie = m.cloneNode(true);
    copie.setAttribute("class", classes);
    return copie;
  }

  function spin() {
    var s = attrs(document.createElementNS(SVG, "svg"), {
      class: "kid-spin", viewBox: "0 0 24 24", "aria-hidden": "true"
    });
    s.appendChild(attrs(document.createElementNS(SVG, "path"), { d: "M12 3a9 9 0 109 9" }));
    return s;
  }

  function scene(source, classes) {
    var d = el("div", "scene");
    d.appendChild(mimo(source, classes));
    return d;
  }

  function note(cls, role, titre, phrase, chip) {
    var n = attrs(el("div", cls), { role: role });
    n.append(el("h2", null, titre), el("p", null, phrase), el("span", "kid-chip " + chip[0], chip[1]));
    return n;
  }

  function bouton(cls, libelle, action) {
    var b = attrs(el("button", cls, libelle), { type: "button" });
    b.addEventListener("click", action);
    return b;
  }

  var MIMO = "#ecran-accueil .hero svg.mimo";
  var MIMO_ETOURDI = "#ecran-erreur svg.mimo";

  var ECRANS = {
    envoi: function () {
      var say = el("p", "say");
      say.append(spin(), document.createTextNode("J'envoie ton idée…"));
      return ["Mimo", [scene(MIMO, "mimo big bob"), say], null];
    },
    "en-cours": function (v) {
      var seg = attrs(el("div", "kid-seg"), {
        role: "progressbar", "aria-label": "Avancement", "aria-valuemin": "0",
        "aria-valuemax": String(ETAPES.length), "aria-valuenow": String(v.etape + 1),
        "aria-valuetext": "Étape " + (v.etape + 1) + " sur " + ETAPES.length
      });
      var film = el("ol", "film");
      ETAPES.forEach(function (nom, j) {
        seg.appendChild(el("i", j <= v.etape ? "on" : null));
        var li = el("li", j < v.etape ? "done" : j === v.etape ? "now" : null);
        if (j === v.etape) li.setAttribute("aria-current", "step");
        li.append(el("span", "fr", String(j + 1)), el("b", null, NOMS[nom]),
          el("span", "st", j < v.etape ? "fait" : j === v.etape ? "en cours" : "bientôt"));
        film.appendChild(li);
      });
      var say = el("p", "say");
      say.append(spin(), document.createTextNode(PHRASES[ETAPES[v.etape]]));
      return ["Ta vidéo arrive", [scene(MIMO, "mimo big bob"), seg, film, say], null];
    },
    "hors-maison": function () {
      var maison = el("div", "home-scene");
      maison.append(mimo(MIMO, "mimo xl"), el("span", "zzz", "Z"), el("span", "zzz b", "z"));
      var n = note("note warn", "status", "Mimo dort un peu", MSG_MAISON, ["warn", "Hors maison"]);
      return ["Mimo", [maison, n], bouton("kid-btn", "Accueil", accueil)];
    },
    erreur: function (v) {
      var n = v.bloque
        ? note("note bad", "alert", "Mimo ne peut pas faire celle-là",
          "Essaie une autre idée.", ["bad", "Autre idée"])
        : note("note bad", "alert", "Oups, la vidéo a raté",
          "Mimo a eu un souci. On réessaie ?", ["bad", "Erreur"]);
      var cta = el("div", "kid-row");
      var r = bouton("kid-btn primary lg", "Réessayer", reessayer);
      if (v.bloque) {
        r.setAttribute("disabled", "");
        cta.appendChild(bouton("kid-btn", "Autre idée", accueil));
      }
      cta.appendChild(r);
      return ["Mimo", [scene(MIMO_ETOURDI, "mimo xl dizzy"), n], cta];
    },
    prete: function () {
      var d = attrs(el("div", "scene"), { role: "status" });
      d.append(mimo(MIMO, "mimo xl bob"), el("h2", null, "Ta vidéo est prête !"));
      return ["Mimo", [d], null];
    }
  };

  var ecran = null;
  var dernier = "";

  function monter() {
    if (ecran) return;
    ecran = attrs(el("section", "kid-screen"), { id: "ecran-statut", "aria-labelledby": "h-statut" });
    var bar = el("header", "kid-bar");
    bar.appendChild(attrs(el("h1", null, "Mimo"), { id: "h-statut" }));
    ecran.append(bar, el("main", "kid-log"), el("div", "kid-cta"));
    document.body.appendChild(ecran);
  }

  function montrer(cible) {
    document.querySelectorAll(".kid-screen").forEach(function (s) {
      s.hidden = s !== cible;
    });
  }

  // Ne redessine que si l'écran ou l'étape change : pas de clignotement ni d'annonce répétée.
  function rendre(v) {
    var cle = [v.ecran, v.etape, v.bloque].join(":");
    if (cle === dernier) return;
    dernier = cle;
    monter();
    var r = ECRANS[v.ecran](v);
    ecran.querySelector("h1").textContent = r[0];
    ecran.querySelector("main").replaceChildren.apply(ecran.querySelector("main"), r[1]);
    var cta = ecran.querySelector(".kid-cta");
    cta.replaceChildren();
    cta.hidden = !r[2];
    if (r[2]) cta.appendChild(r[2]);
    montrer(ecran);
  }

  // --- Échanges avec le serveur ---
  var texte = null;
  var jobId = null;
  var panneReseau = false;
  var minuteur = null;

  function lire(r) {
    if (!r.ok) throw new Error("http " + r.status);
    return r.json();
  }

  function panne() {
    panneReseau = true;
    rendre({ ecran: "erreur" });
  }

  function traiter(rep) {
    if (typeof rep.job_id === "string") jobId = rep.job_id;
    var v = vue(rep);
    rendre(v);
    if (v.ecran === "en-cours") {
      minuteur = setTimeout(interroger, POLL_MS);
    } else if (v.bloque) {
      texte = null; // dictée refusée : on ne la renverra pas
    } else if (v.ecran === "prete") {
      texte = null;
      document.dispatchEvent(new CustomEvent("mimo:video-prete", { detail: { job_id: jobId } }));
    }
  }

  function interroger() {
    fetch("/api/jobs/" + encodeURIComponent(jobId), { cache: "no-store" })
      .then(lire)
      .then(traiter, panne);
  }

  function envoyer(t) {
    clearTimeout(minuteur);
    texte = t;
    jobId = null;
    panneReseau = false;
    dernier = "";
    rendre(vue(null));
    fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: t })
    })
      .then(lire)
      .then(traiter, panne);
  }

  function reessayer() {
    if (panneReseau && jobId) {
      panneReseau = false;
      interroger(); // le polling reprend là où il s'était arrêté
    } else if (texte) {
      envoyer(texte);
    } else {
      accueil();
    }
  }

  function accueil() {
    clearTimeout(minuteur);
    dernier = "";
    montrer(document.getElementById("ecran-accueil"));
  }

  var api = { vue: vue, envoyer: envoyer, ETAPES: ETAPES, POLL_MS: POLL_MS };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.MimoStatus = api;
  }
})(typeof window !== "undefined" ? window : this);
