// Actualisation de /progress toutes les 60 s (sans dépendance : ni CDN, ni bibliothèque).
(() => {
  const live = document.getElementById("live");
  if (!live) return;
  const refresh = async () => {
    if (document.hidden || live.contains(document.activeElement)) return; // ne pas voler le focus
    live.setAttribute("aria-busy", "true");
    try {
      const res = await fetch("/progress", { credentials: "same-origin", cache: "no-store" });
      if (res.redirected) return location.assign("/login");
      if (!res.ok) return;
      const next = new DOMParser().parseFromString(await res.text(), "text/html").getElementById("live");
      if (next) {
        live.innerHTML = next.innerHTML;
        live.dataset.refreshed = "1"; // les animations d'entrée ne rejouent pas
      }
    } catch (_) {
      // réseau coupé : l'affichage actuel reste
    } finally {
      live.removeAttribute("aria-busy");
    }
  };
  setInterval(refresh, 60000);
})();
