// Mini DOM pour les tests node de l'UI enfant (#214) : exécute les VRAIS scripts de static/ (comme
// index.html les charge) sans navigateur. Mocks seulement : fetch, micro, minuteurs. Aucun réseau.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = path.join(__dirname, "..", "static");
const SCRIPTS = ["status.js", "player.js", "shell.js", "dictation.js"]; // ordre d'index.html

class Evt {
  constructor(type, init) {
    this.type = type;
    this.detail = init && init.detail;
    this.defaultPrevented = false;
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
}

class Target {
  constructor() {
    this.listeners = {};
  }
  addEventListener(type, f) {
    (this.listeners[type] = this.listeners[type] || []).push(f);
  }
  dispatchEvent(e) {
    (this.listeners[e.type] || []).slice().forEach((f) => f(e));
    return true;
  }
}

// --- Sélecteurs : tag, #id, .classe, [attr], [attr=val], séparés par des espaces (descendants) ---
const TOKEN = /^(?:([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|([^\]]*)))?\])/;

function compound(texte) {
  const c = { tag: null, id: null, cls: [], attrs: [] };
  let reste = texte;
  while (reste) {
    const m = TOKEN.exec(reste);
    if (!m) throw new Error("sélecteur non géré : " + texte);
    if (m[1]) c.tag = m[1];
    else if (m[2]) c.id = m[2];
    else if (m[3]) c.cls.push(m[3]);
    else c.attrs.push([m[4], m[5] !== undefined ? m[5] : m[6]]);
    reste = reste.slice(m[0].length);
  }
  return c;
}

function correspond(n, c) {
  if (n.tag === "#text") return false;
  if (c.tag && n.tag !== c.tag) return false;
  if (c.id && n.id !== c.id) return false;
  const classes = String(n.className || "").split(/\s+/);
  if (!c.cls.every((k) => classes.includes(k))) return false;
  return c.attrs.every(([k, v]) => n.getAttribute(k) !== null && (v === undefined || n.getAttribute(k) === v));
}

function selectionne(n, parts) {
  if (!correspond(n, parts[parts.length - 1])) return false;
  let i = parts.length - 2;
  for (let a = n.parentNode; a && i >= 0; a = a.parentNode) {
    if (correspond(a, parts[i])) i--;
  }
  return i < 0;
}

class Node extends Target {
  constructor(tag) {
    super();
    this.tag = tag;
    this.childNodes = [];
    this.parentNode = null;
    this.attrs = {};
    this.className = "";
    this.hidden = false;
    this.style = {};
    this.text = "";
    this.paused = false;
  }
  get id() {
    return this.attrs.id || "";
  }
  get children() {
    return this.childNodes.filter((n) => n.tag !== "#text");
  }
  get firstChild() {
    return this.childNodes[0] || null;
  }
  get lastChild() {
    return this.childNodes[this.childNodes.length - 1] || null;
  }
  get form() {
    for (let a = this.parentNode; a; a = a.parentNode) if (a.tag === "form") return a;
    return null;
  }
  get textContent() {
    return this.tag === "#text" ? this.text : this.childNodes.map((c) => c.textContent).join("");
  }
  set textContent(t) {
    if (this.tag === "#text") this.text = String(t);
    else this.replaceChildren(...(String(t) === "" ? [] : [textNode(String(t))]));
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === "class") this.className = String(v);
  }
  getAttribute(k) {
    return k in this.attrs ? this.attrs[k] : null;
  }
  appendChild(c) {
    if (c.parentNode) c.remove();
    c.parentNode = this;
    this.childNodes.push(c);
    return c;
  }
  append(...cs) {
    cs.forEach((c) => this.appendChild(typeof c === "string" ? textNode(c) : c));
  }
  replaceChildren(...cs) {
    this.childNodes.forEach((c) => (c.parentNode = null));
    this.childNodes = [];
    this.append(...cs);
  }
  insertBefore(n, ref) {
    if (n.parentNode) n.remove();
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    n.parentNode = this;
    if (i < 0) this.childNodes.push(n);
    else this.childNodes.splice(i, 0, n);
    return n;
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  contains(n) {
    for (let a = n; a; a = a.parentNode) if (a === this) return true;
    return false;
  }
  cloneNode(deep) {
    const c = new Node(this.tag);
    c.doc = this.doc;
    Object.assign(c.attrs, this.attrs);
    c.className = this.className;
    c.text = this.text;
    if (deep) this.childNodes.forEach((k) => c.appendChild(k.cloneNode(true)));
    return c;
  }
  all() {
    return this.childNodes.flatMap((c) => [c, ...c.all()]);
  }
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/).map(compound);
    return this.all().filter((n) => selectionne(n, parts));
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  click() {
    this.dispatchEvent(new Evt("click"));
  }
  focus() {
    this.doc.activeElement = this;
  }
  pause() {
    this.paused = true;
  }
}

function textNode(t) {
  const n = new Node("#text");
  n.text = String(t);
  return n;
}

function h(doc, tag, attrs, ...kids) {
  const n = new Node(tag);
  n.doc = doc;
  Object.keys(attrs || {}).forEach((k) => n.setAttribute(k, attrs[k]));
  n.append(...kids);
  return n;
}

// Reproduit la structure de index.html utilisée par les scripts (ids, classes, aria-label).
function construire(doc) {
  const mimo = (cls) => h(doc, "svg", { class: cls });
  const accueil = h(doc, "section", { class: "kid-screen", id: "ecran-accueil" },
    h(doc, "header", { class: "kid-bar" }, h(doc, "h1", { id: "h-accueil" }, "Mimo")),
    h(doc, "main", { class: "kid-log" },
      h(doc, "div", { class: "hero" }, mimo("mimo xl bob"), h(doc, "h2", {}, "On fait une vidéo ?")),
      h(doc, "div", { class: "kid-theme", role: "radiogroup" },
        ...["Nuit", "Soleil", "Menthe"].map((n) => h(doc, "button", { role: "radio", "aria-checked": "false" }, n)))),
    h(doc, "form", { class: "kid-composer" },
      h(doc, "textarea", { id: "idee", name: "idee" }),
      h(doc, "button", { type: "button", "aria-label": "Parler" }),
      h(doc, "button", { type: "submit", "aria-label": "Envoyer" })));
  const erreur = h(doc, "section", { class: "kid-screen", id: "ecran-erreur" },
    h(doc, "header", { class: "kid-bar" }, h(doc, "h1", { id: "h-erreur" }, "Mimo")),
    h(doc, "main", { class: "kid-log" },
      h(doc, "div", { class: "scene" }, mimo("mimo xl dizzy")),
      h(doc, "div", { class: "note bad", role: "alert" },
        h(doc, "h2", {}, "Oups, pas de connexion"), h(doc, "p", {}, "Mimo n'arrive pas à te répondre."),
        h(doc, "span", { class: "kid-chip bad" }, h(doc, "svg", {}), "Hors ligne"))),
    h(doc, "div", { class: "kid-cta" }, h(doc, "button", { type: "button", class: "kid-btn primary lg" }, "Réessayer")));
  return [accueil, erreur];
}

class FauxMediaRecorder extends Target {
  constructor(stream, options) {
    super();
    this.stream = stream;
    this.mimeType = (options && options.mimeType) || "";
    this.state = "inactive";
  }
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    this.dispatchEvent(Object.assign(new Evt("dataavailable"), { data: new Blob(["audio-synthetique"]) }));
    this.dispatchEvent(new Evt("stop"));
  }
  static isTypeSupported() {
    return true;
  }
}

// opts.routes(url, init) -> { status, corps } | { reseau: false } (fetch rejeté) | undefined ; opts.enLigne (défaut true).
function demarrer(opts) {
  const o = opts || {};
  const doc = Object.assign(new Target(), { dir: "", activeElement: null, visibilityState: "visible" });
  const html = h(doc, "html", {});
  const body = h(doc, "body", {});
  html.append(body);
  doc.documentElement = html;
  doc.body = body;
  doc.createElement = (t) => h(doc, t, {});
  doc.createElementNS = (_, t) => h(doc, t, {});
  doc.createTextNode = textNode;
  doc.getElementById = (id) => html.querySelector("#" + id);
  doc.querySelector = (s) => html.querySelector(s);
  doc.querySelectorAll = (s) => html.querySelectorAll(s);
  construire(doc).forEach((s) => body.appendChild(s));

  const appels = []; // { url, method, json, formdata } : jamais de texte d'enfant ailleurs que dans json
  const minuteurs = [];
  const flux = { getTracks: () => [{ stop() {} }] };
  const win = Object.assign(new Target(), {
    document: doc, Evt, CustomEvent: Evt, FormData, Blob, FauxMediaRecorder,
    MediaRecorder: FauxMediaRecorder, isSecureContext: true,
    navigator: { onLine: o.enLigne !== false, mediaDevices: { getUserMedia: () => Promise.resolve(flux) } },
    location: { reload() {} }, localStorage: { setItem() {} },
    fetch(url, init) {
      const i = init || {};
      const json = typeof i.body === "string" ? JSON.parse(i.body) : undefined;
      appels.push({ url, method: i.method || "GET", json, formdata: i.body instanceof FormData, headers: i.headers });
      const rep = (o.routes && o.routes(url, i)) || { status: 404, corps: null };
      if (rep.reseau === false) return Promise.reject(new TypeError("Failed to fetch")); // réseau coupé
      return Promise.resolve({ ok: rep.status < 400, status: rep.status, json: () => Promise.resolve(rep.corps) });
    },
    setTimeout(f, ms) {
      minuteurs.push({ f, ms });
      return minuteurs.length;
    },
    clearTimeout(id) {
      if (minuteurs[id - 1]) minuteurs[id - 1] = { f() {}, ms: -1 };
    },
  });
  win.window = win;
  vm.createContext(win);
  SCRIPTS.forEach((s) => vm.runInContext(fs.readFileSync(path.join(STATIC, s), "utf8"), win, { filename: s }));

  const api = {
    win, doc, appels, minuteurs,
    q: (s) => doc.querySelector(s),
    qa: (s) => doc.querySelectorAll(s),
    visibles: () => doc.querySelectorAll(".kid-screen").filter((s) => !s.hidden).map((s) => s.id || s.className),
    // Bouton portant ce texte dans un écran visible (comme l'enfant ne peut toucher que ceux-là).
    bouton: (texte) => doc.querySelectorAll("button").find((b) => {
      for (let n = b; n; n = n.parentNode) if (n.hidden) return false;
      return b.textContent.includes(texte);
    }) || null,
    flush: async () => {
      for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r));
    },
    // Déclenche le prochain minuteur de sondage (2 s) : l'orchestrateur a avancé.
    sonder: async () => {
      const i = minuteurs.findIndex((m) => m.ms === 2000);
      if (i < 0) return false;
      const [m] = minuteurs.splice(i, 1);
      m.f();
      await api.flush();
      return true;
    },
    // Retour au premier plan (ou mise en arrière-plan) : visibilitychange sur document.
    visibilite: (etat) => {
      doc.visibilityState = etat;
      doc.dispatchEvent(new Evt("visibilitychange"));
    },
    reseau: (enLigne) => {
      win.navigator.onLine = enLigne;
      win.dispatchEvent(new Evt(enLigne ? "online" : "offline"));
    },
    // Micro -> « J'ai fini » -> réponse de /api/dictation : l'écran « Tu as dit » est affiché.
    dicter: async () => {
      doc.querySelector('.kid-composer [aria-label="Parler"]').click();
      await api.flush();
      api.bouton("J'ai fini").click();
      await api.flush();
    },
  };
  return api;
}

module.exports = { demarrer, Evt };
