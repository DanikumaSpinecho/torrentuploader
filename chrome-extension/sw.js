// Torrent Uploader — service worker (Manifest V3).
//
// Rôle : faire parvenir à l'application locale les torrents rencontrés dans le
// navigateur. Deux sources :
//   1. un téléchargement de .torrent qui se termine (capture automatique) ;
//   2. le clic droit « Envoyer vers la seedbox » sur un lien.
//
// Deux transports, essayés dans cet ordre :
//   A. la messagerie native — aucun réglage, et l'application est lancée si
//      elle est fermée ;
//   B. en secours, le service HTTP local de l'application, si un jeton a été
//      saisi dans les options.
//
// Rappel MV3 : ce worker n'est pas persistant. Les écouteurs sont enregistrés au
// premier niveau du module, et rien n'est gardé en mémoire entre deux réveils.

const HOST = "com.torrentuploader.host";
const DEFAULTS = {
  autoCapture: true,          // envoyer les .torrent dès la fin du téléchargement
  removeAfterImport: false,   // puis retirer le fichier du dossier de téléchargement
  notify: true,               // notifications du système
  token: "",                  // connexion manuelle (secours)
  port: 38471,
};
const MAX_TORRENT_BYTES = 8 * 1024 * 1024;

async function getCfg() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(DEFAULTS)) };
}

// --------------------------------------------------------------------------
//  Transports
// --------------------------------------------------------------------------

async function viaNative(message) {
  try {
    const reply = await chrome.runtime.sendNativeMessage(HOST, message);
    return { transport: "native", ...(reply || {}), ok: !!(reply && reply.ok) };
  } catch (err) {
    // Hôte non déclaré, interdit ou planté : la liaison automatique n'est pas disponible.
    return { transport: "native", ok: false, unavailable: true, error: String((err && err.message) || err) };
  }
}

async function viaHttp(route, payload, cfg) {
  if (!cfg.token) return null;
  const url = `http://127.0.0.1:${cfg.port}${route}`;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, ...payload }),
    });
    const data = await resp.json().catch(() => ({}));
    return { transport: "http", ...data, status: resp.status, ok: resp.ok && data.ok !== false };
  } catch (err) {
    return { transport: "http", ok: false, unreachable: true, error: String((err && err.message) || err) };
  }
}

/** Envoie une demande à l'application : "ping", "show" ou "add" (avec items). */
async function request(action, items) {
  const cfg = await getCfg();
  const native = await viaNative(items ? { action, items } : { action });
  if (!native.unavailable) return native;
  const http = await viaHttp("/" + action, items ? { items } : {}, cfg);
  if (http) return { ...http, nativeError: native.error };
  return native;
}

// --------------------------------------------------------------------------
//  Retour visuel
// --------------------------------------------------------------------------

function explain(result) {
  if (result.ok) {
    const n = result.accepted || 0;
    const what = n > 1 ? `${n} torrents envoyés` : "Torrent envoyé";
    return `${what} à Torrent Uploader${result.launched ? " (application lancée)" : ""}.`;
  }
  if (result.transport === "native" && result.unavailable) {
    return "Liaison avec l'application introuvable. Ouvrez Torrent Uploader, bouton « Intégration », puis « Activer ».";
  }
  if (result.transport === "http" && result.unreachable) {
    return "Application injoignable : ouvrez Torrent Uploader, ou activez l'intégration pour qu'elle se lance seule.";
  }
  if (result.transport === "http" && result.status === 403) {
    return "Jeton refusé : recopiez-le depuis l'application (Intégration > Connexion manuelle).";
  }
  const detail = (result.errors && result.errors[0]) || result.error;
  return detail ? `Refusé par l'application : ${detail}` : "L'application n'a pas accepté l'envoi.";
}

async function feedback(result) {
  const ok = !!result.ok;
  try {
    await chrome.action.setBadgeBackgroundColor({ color: ok ? "#12824C" : "#C42B1C" });
    await chrome.action.setBadgeText({ text: ok ? "✓" : "!" });
    setTimeout(() => chrome.action.setBadgeText({ text: "" }).catch(() => {}), ok ? 4000 : 12000);
  } catch (_) { /* le badge est un confort */ }
  await chrome.storage.session.set({ lastResult: { ok, message: explain(result), at: Date.now() } }).catch(() => {});
  const cfg = await getCfg();
  if (!cfg.notify) return;
  try {
    chrome.notifications.create("", {
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon128.png"),
      title: ok ? "Torrent Uploader" : "Torrent Uploader — envoi impossible",
      message: explain(result),
    }, () => void chrome.runtime.lastError);
  } catch (_) { /* notifications indisponibles : le badge suffit */ }
}

async function sendItems(items) {
  const result = await request("add", items);
  await feedback(result);
  return result;
}

// --------------------------------------------------------------------------
//  1. Capture des téléchargements
// --------------------------------------------------------------------------
//
// À la fin d'un téléchargement, Chrome connaît le chemin complet du fichier,
// où que l'utilisateur ait choisi de l'enregistrer. On transmet ce chemin :
// l'application lit le fichier elle-même. Pas de second téléchargement, donc
// aucun problème de cookies, de jeton à usage unique ou de tracker privé.

chrome.downloads.onChanged.addListener(async (delta) => {
  if (!delta.state || delta.state.current !== "complete") return;
  const [item] = await chrome.downloads.search({ id: delta.id });
  if (!item || !/\.torrent$/i.test(item.filename || "")) return;

  const cfg = await getCfg();
  const ours = item.byExtensionId === chrome.runtime.id;   // lancé par notre menu contextuel
  if (!cfg.autoCapture && !ours) return;

  const consume = !!cfg.removeAfterImport;
  const result = await sendItems([{ path: item.filename, consume }]);
  if (result.ok && consume) {
    // L'application a fait sa propre copie : le fichier peut quitter le disque.
    try { await chrome.downloads.removeFile(item.id); } catch (_) { /* déjà absent */ }
    try { await chrome.downloads.erase({ id: item.id }); } catch (_) { /* sans importance */ }
  }
});

// --------------------------------------------------------------------------
//  2. Menu contextuel
// --------------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    // « magnet:* » n'est pas un motif de correspondance valide : l'entrée est
    // proposée sur tous les liens, et le tri se fait au clic.
    chrome.contextMenus.create({
      id: "tu-link",
      title: "Envoyer vers la seedbox (Torrent Uploader)",
      contexts: ["link"],
    });
    chrome.contextMenus.create({
      id: "tu-selection",
      title: "Envoyer le lien sélectionné vers la seedbox",
      contexts: ["selection"],
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const target = (info.menuItemId === "tu-link" ? info.linkUrl : info.selectionText || "").trim();
  if (!target) return;

  if (/^magnet:\?/i.test(target)) {
    await sendItems([target]);
    return;
  }
  if (!/^https?:\/\//i.test(target)) {
    await feedback({ ok: false, error: "ce lien n'est ni un lien magnet ni une adresse http(s)" });
    return;
  }

  // a) Lire le fichier depuis la page : la requête part avec la session du site,
  //    ce qui est indispensable sur un tracker privé.
  const read = await readInPage(target, tab, info.frameId);
  if (read && read.ok) {
    await sendItems([{ name: read.name, data_b64: read.data_b64 }]);
    return;
  }
  if (read && read.reason === "not-torrent") {
    await feedback({ ok: false, error: "ce lien ne mène pas à un fichier .torrent" });
    return;
  }
  // b) Sinon, laisser le navigateur le télécharger : la capture ci-dessus prendra le relais.
  try {
    await chrome.downloads.download({ url: target, saveAs: false });
  } catch (_) {
    // c) Dernier recours : transmettre l'adresse, la seedbox ira chercher le fichier.
    await sendItems([target]);
  }
});

async function readInPage(url, tab, frameId) {
  if (!tab || typeof tab.id !== "number" || tab.id < 0) return null;
  try {
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: tab.id, frameIds: [typeof frameId === "number" ? frameId : 0] },
      func: fetchTorrentInPage,
      args: [url, MAX_TORRENT_BYTES],
    });
    return (injection && injection.result) || null;
  } catch (_) {
    return null;                // page protégée, cadre d'une autre origine...
  }
}

/** Exécutée DANS la page : doit rester autonome (aucune variable extérieure). */
async function fetchTorrentInPage(url, maxBytes) {
  try {
    const resp = await fetch(url, { credentials: "include" });
    if (!resp.ok) return null;
    const bytes = new Uint8Array(await resp.arrayBuffer());
    if (!bytes.length || bytes.length > maxBytes) return null;
    if (bytes[0] !== 0x64) return { ok: false, reason: "not-torrent" };   // bencode commence par « d »

    let name = "";
    const disposition = resp.headers.get("content-disposition") || "";
    const star = /filename\*\s*=\s*(?:UTF-8'')?([^;]+)/i.exec(disposition);
    const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition);
    try {
      if (star) name = decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
      else if (plain) name = plain[1].trim();
      else name = decodeURIComponent(new URL(url, location.href).pathname.split("/").pop() || "");
    } catch (_) { /* nom par défaut ci-dessous */ }

    let binary = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return { ok: true, name: name || "telechargement.torrent", data_b64: btoa(binary) };
  } catch (_) {
    return null;                // autre origine sans CORS, réseau coupé...
  }
}

// --------------------------------------------------------------------------
//  Messages de la fenêtre de l'extension et de la page d'options
// --------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const type = message && message.type;
  if (type === "status") {
    request("ping").then(sendResponse);
    return true;                // réponse asynchrone
  }
  if (type === "show") {
    request("show").then(sendResponse);
    return true;
  }
  if (type === "diagnose") {
    (async () => {
      const cfg = await getCfg();
      const native = await viaNative({ action: "ping" });
      const http = await viaHttp("/ping", {}, cfg);
      sendResponse({ native, http });
    })();
    return true;
  }
  return false;
});
