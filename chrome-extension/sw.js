// Service worker MV3 — relaie les liens torrent/magnet vers l'application locale.
//
// Rappel MV3 : ce worker n'est pas persistant. Les menus contextuels doivent
// donc etre (re)crees dans onInstalled, jamais au chargement du module.

const DEFAULTS = { port: 38471, token: "", autoCapture: false };

async function getCfg() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored };
}

function notify(title, message) {
  try {
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icon128.png",
      title,
      message: String(message).slice(0, 300),
    });
  } catch (_) {
    // les notifications sont un confort, jamais un point de rupture
  }
}

/** Envoie une liste d'elements a l'application locale. */
async function sendItems(items) {
  const cfg = await getCfg();
  if (!cfg.token) {
    notify("Torrent Uploader", "Jeton absent : ouvrez les options de l'extension.");
    return false;
  }
  const url = `http://127.0.0.1:${cfg.port}/add`;
  let resp;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, items }),
    });
  } catch (err) {
    // Chemin d'erreur distinct : une coupure ici vient du reseau local
    // (application fermee, port different, ou restriction Local Network Access),
    // pas d'une erreur de l'utilisateur.
    notify(
      "Torrent Uploader injoignable",
      `Impossible de contacter ${url}. L'application est-elle ouverte ? (${err.message})`
    );
    return false;
  }
  if (resp.status === 403) {
    notify("Torrent Uploader", "Jeton refuse : recopiez-le depuis l'application.");
    return false;
  }
  if (!resp.ok) {
    notify("Torrent Uploader", `Refus du service local (HTTP ${resp.status}).`);
    return false;
  }
  const data = await resp.json().catch(() => ({}));
  if (data.accepted) {
    notify("Torrent Uploader", `${data.accepted} element(s) envoye(s).`);
    return true;
  }
  notify("Torrent Uploader", (data.errors && data.errors[0]) || "Element refuse.");
  return false;
}

// --------------------------------------------------------------------------
//  Menus contextuels
// --------------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    // Un seul element sur les liens : « magnet:* » n'est pas un motif de
    // correspondance legal (les schemas admis se limitent a http/https/file/*),
    // le filtrage se fait donc dans onClicked.
    chrome.contextMenus.create({
      id: "tu-link",
      title: "Envoyer vers la seedbox (Torrent Uploader)",
      contexts: ["link"],
    });
    chrome.contextMenus.create({
      id: "tu-selection",
      title: "Envoyer le lien selectionne vers la seedbox",
      contexts: ["selection"],
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  let target = "";
  if (info.menuItemId === "tu-link") {
    target = info.linkUrl || "";
  } else if (info.menuItemId === "tu-selection") {
    target = (info.selectionText || "").trim();
  }
  if (!target) return;

  if (target.toLowerCase().startsWith("magnet:")) {
    await sendItems([target]);
    return;
  }
  if (/^https?:\/\//i.test(target)) {
    // Un .torrent derriere une authentification ne peut pas etre recupere par
    // la seedbox : on tente de le lire ici, avec les cookies du site.
    const bytes = await fetchTorrentBytes(target);
    if (bytes) {
      await sendItems([bytes]);
    } else {
      await sendItems([target]); // repli : la seedbox telechargera l'URL elle-meme
    }
    return;
  }
  notify("Torrent Uploader", "Ce lien n'est ni un magnet ni une URL exploitable.");
});

// --------------------------------------------------------------------------
//  Lecture du .torrent avec les cookies de l'onglet (trackers prives)
// --------------------------------------------------------------------------

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function fileNameFromUrl(url) {
  try {
    const path = new URL(url).pathname;
    return decodeURIComponent(path.split("/").pop() || "") || "telechargement.torrent";
  } catch (_) {
    return "telechargement.torrent";
  }
}

/** Retourne {name, data_b64} ou null si la lecture directe est impossible. */
async function fetchTorrentBytes(url) {
  const granted = await chrome.permissions.contains({ origins: ["<all_urls>"] });
  if (!granted) return null;
  try {
    const resp = await fetch(url, { credentials: "include" });
    if (!resp.ok) return null;
    const buf = await resp.arrayBuffer();
    if (buf.byteLength === 0 || buf.byteLength > 8 * 1024 * 1024) return null;
    if (new Uint8Array(buf)[0] !== 0x64) return null; // bencode commence par 'd'
    return { name: fileNameFromUrl(url), data_b64: toBase64(buf) };
  } catch (_) {
    return null;
  }
}

// --------------------------------------------------------------------------
//  Capture automatique des telechargements (optionnelle, desactivee par defaut)
// --------------------------------------------------------------------------

function looksLikeTorrent(item) {
  const url = item.finalUrl || item.url || "";
  if (/\.torrent(\?|$)/i.test(url)) return true;
  return (item.mime || "").toLowerCase() === "application/x-bittorrent";
}

chrome.downloads.onCreated.addListener(async (item) => {
  const cfg = await getCfg();
  if (!cfg.autoCapture) return;
  // item.filename est vide a ce stade : le nom n'est pas encore determine.
  if (!looksLikeTorrent(item)) return;

  const payload = await fetchTorrentBytes(item.finalUrl || item.url);
  if (!payload) {
    // Sans acces au site, on laisse Chrome telecharger normalement ; la
    // surveillance de dossier de l'application prendra le relais.
    return;
  }
  const ok = await sendItems([payload]);
  if (ok) {
    try {
      await chrome.downloads.cancel(item.id);
      await chrome.downloads.erase({ id: item.id });
    } catch (_) {
      // le telechargement etait deja termine : sans importance
    }
  }
});

// Test de connexion demande par la page d'options.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "ping") {
    (async () => {
      const cfg = await getCfg();
      try {
        const resp = await fetch(`http://127.0.0.1:${cfg.port}/ping`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: cfg.token }),
        });
        const data = await resp.json().catch(() => ({}));
        sendResponse({ ok: resp.ok, status: resp.status, data });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true; // reponse asynchrone
  }
});
