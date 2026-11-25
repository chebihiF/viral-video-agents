// Dictée au micro (#161) : MediaRecorder vers POST /api/dictation, sans service tiers.
// L'audio est envoyé en HTTPS local, puis oublié : rien dans le stockage du navigateur, aucun service tiers.
// Le texte dicté n'est inséré que par textContent / value, jamais comme HTML.
(function () {
  var MAX_MS = 30000; // ~1 Mo max côté serveur
  var TEXTE_MAX = 500; // contrat kid-job : au-delà, POST /api/jobs répond 400
  var accueil = document.getElementById("ecran-accueil");
  var erreur = document.getElementById("ecran-erreur");
  var micro = document.querySelector('.kid-composer [aria-label="Parler"]');
  var idee = document.getElementById("idee");
  if (!accueil || !erreur || !micro || !idee) return;

  var session = null; // { recorder, stream, chunks, timer, annule }
  var ecran = null;

  function el(tag, classe, texte) {
    var n = document.createElement(tag);
    if (classe) n.className = classe;
    if (texte) n.textContent = texte;
    return n;
  }

  function bouton(classe, texte, onclick) {
    var b = el("button", "kid-btn " + classe, texte);
    b.type = "button";
    b.addEventListener("click", onclick);
    return b;
  }

  // ---- État Erreur : on réutilise l'écran de la maquette, jamais de plantage ----
  var titre = erreur.querySelector("h2");
  var detail = erreur.querySelector("p");
  var puce = erreur.querySelector(".kid-chip");
  var origine = [titre.textContent, detail.textContent, puce.lastChild.textContent];

  function restaurerErreur() {
    titre.textContent = origine[0];
    detail.textContent = origine[1];
    puce.lastChild.textContent = origine[2];
  }

  // Le réseau change : la coquille bascule d'écran, on retire l'écoute (micro coupé, audio oublié).
  window.addEventListener("online", function () {
    if (ecran) retourAccueil(); // écoute interrompue : on ne laisse pas un écran vide
    else fermerEcoute();
  });
  window.addEventListener("offline", function () {
    fermerEcoute();
    restaurerErreur();
  });

  function afficherErreur(t, d, p) {
    fermerEcoute();
    titre.textContent = t;
    detail.textContent = d;
    puce.lastChild.textContent = p;
    accueil.hidden = true;
    erreur.hidden = false;
  }

  // Erreurs de dictée (#234) : phrases courtes, jamais de code technique ni de texte dicté à l'écran.
  var ERREURS = {
    vide: ["Je n'ai rien entendu", "Parle un peu plus fort, puis réessaie"],
    long: ["C'est un peu long", "Dis-moi une idée plus courte"],
    sourd: ["Mimo ne peut pas écouter", "Demande à un grand de m'aider"],
    autre: ["Oups, je n'ai pas compris", "Mimo n'a pas réussi à t'écouter. Réessaie."],
  };

  // Réponse de /api/dictation -> clé de ERREURS ; "" = texte exploitable.
  function classer(rep, texte) {
    if (rep.status === 413 && rep.corps && rep.corps.error === "audio_trop_gros") return "long";
    if (rep.status === 503 && rep.corps && rep.corps.error === "asr_indisponible") return "sourd";
    if (!rep.ok) return "autre";
    if (rep.corps && typeof rep.corps.text === "string") return texte ? "" : "vide";
    return "autre";
  }

  function erreurDictee(cle) {
    afficherErreur(ERREURS[cle][0], ERREURS[cle][1], "Dictée");
  }

  function erreurMicro() {
    afficherErreur("Je ne t'entends pas", "Le micro est fermé. Autorise-le, puis réessaie.", "Micro");
  }

  // ---- États Écoute / Envoi / Résultat ----
  function construireEcoute() {
    var s = el("section", "kid-screen");
    s.setAttribute("aria-labelledby", "h-ecoute");
    var bar = el("header", "kid-bar");
    var h1 = el("h1", "", "Mimo");
    h1.id = "h-ecoute";
    bar.appendChild(h1);
    var main = el("main", "kid-log");
    var groupe = el("div", "kid-listen");
    groupe.setAttribute("role", "group");
    groupe.setAttribute("aria-label", "Dictée");
    var live = el("div", "live");
    live.appendChild(el("span", "rec-dot"));
    var etat = el("span", "", "Je t'écoute");
    live.appendChild(etat);
    var scene = el("div", "stage");
    var mimo = accueil.querySelector("svg.mimo");
    if (mimo) scene.appendChild(mimo.cloneNode(true));
    for (var i = 0; i < 3; i++) scene.appendChild(el("i"));
    var barres = el("div", "kid-bars");
    barres.setAttribute("aria-hidden", "true");
    for (var j = 0; j < 7; j++) {
      var b = el("i");
      b.style.height = 12 + ((j * 7) % 5) * 7 + "px";
      barres.appendChild(b);
    }
    var entendu = el("p", "heard");
    entendu.setAttribute("aria-live", "polite");
    var rangee = el("div", "kid-row");
    groupe.append(live, scene, barres, entendu, rangee);
    main.appendChild(groupe);
    s.append(bar, main);
    return { racine: s, etat: etat, live: live, barres: barres, entendu: entendu, rangee: rangee };
  }

  function montrerEcoute() {
    ecran = construireEcoute();
    accueil.hidden = true;
    erreur.hidden = true;
    accueil.parentNode.insertBefore(ecran.racine, accueil);
    ecran.rangee.append(
      bouton("", "Annuler", annuler),
      bouton("primary", "J'ai fini", terminer)
    );
    ecran.rangee.lastChild.focus();
  }

  function fermerEcoute() {
    if (ecran) {
      ecran.racine.remove();
      ecran = null;
    }
    if (session) {
      clearTimeout(session.timer);
      session.stream.getTracks().forEach(function (t) {
        t.stop();
      });
      session = null; // l'audio enregistré n'est plus référencé nulle part
    }
  }

  function retourAccueil(focus) {
    fermerEcoute();
    erreur.hidden = true;
    accueil.hidden = false;
    if (focus) focus.focus();
  }

  function annuler() {
    if (session && session.recorder.state !== "inactive") {
      session.annule = true;
      session.recorder.stop();
    }
    retourAccueil(micro);
  }

  function terminer() {
    if (session && session.recorder.state === "recording") session.recorder.stop();
  }

  function envoi(blob) {
    var mon = ecran; // la réponse n'est utilisée que si cet écran est toujours affiché
    ecran.etat.textContent = "J'envoie…";
    ecran.live.firstChild.hidden = true;
    ecran.barres.hidden = true;
    ecran.rangee.replaceChildren();
    var fd = new FormData();
    fd.append("audio", blob, "dictee");
    fetch("/api/dictation", { method: "POST", body: fd, cache: "no-store", credentials: "omit" })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, status: r.status, corps: j };
        });
      })
      .then(function (rep) {
        var texte = rep.corps && typeof rep.corps.text === "string" ? rep.corps.text.trim().slice(0, TEXTE_MAX) : "";
        var cle = classer(rep, texte);
        if (ecran === mon) {
          if (cle) erreurDictee(cle);
          else resultat(texte);
        }
      })
      .catch(function () {
        if (ecran === mon) erreurDictee("autre");
      });
  }

  // Rien ne part avant ce clic : l'écran d'écoute est retiré, MimoStatus (status.js) envoie puis suit.
  function valider(texte) {
    fermerEcoute();
    MimoStatus.envoyer(texte);
  }

  function resultat(texte) {
    if (!ecran) return;
    ecran.etat.textContent = "Tu as dit";
    ecran.entendu.textContent = texte;
    ecran.rangee.append(
      bouton("", "Annuler", annuler),
      bouton("primary", "✅ C'est bon", function () {
        valider(texte);
      })
    );
    ecran.rangee.lastChild.focus();
  }

  function choisirType() {
    var types = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
    for (var i = 0; i < types.length; i++) {
      if (MediaRecorder.isTypeSupported(types[i])) return types[i];
    }
    return "";
  }

  function demarrer(stream) {
    var type = choisirType();
    var options = { audioBitsPerSecond: 32000 };
    if (type) options.mimeType = type;
    var recorder;
    try {
      recorder = new MediaRecorder(stream, options);
    } catch (e) {
      stream.getTracks().forEach(function (t) {
        t.stop();
      });
      erreurMicro();
      return;
    }
    session = { recorder: recorder, stream: stream, chunks: [], timer: null, annule: false };
    var courante = session;
    recorder.addEventListener("dataavailable", function (e) {
      if (e.data && e.data.size) courante.chunks.push(e.data);
    });
    recorder.addEventListener("stop", function () {
      var blob = courante.annule ? null : new Blob(courante.chunks, { type: recorder.mimeType || type || "audio/webm" });
      courante.stream.getTracks().forEach(function (t) {
        t.stop();
      });
      courante.chunks = [];
      if (blob && session === courante && ecran) envoi(blob);
    });
    montrerEcoute();
    recorder.start();
    courante.timer = setTimeout(terminer, MAX_MS);
  }

  micro.addEventListener("click", function () {
    var ok = window.isSecureContext && navigator.mediaDevices && window.MediaRecorder;
    if (!ok) {
      erreurMicro();
      return;
    }
    if (session) return;
    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then(demarrer)
      .catch(erreurMicro);
  });
})();
