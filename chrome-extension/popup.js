// Fenêtre de l'extension : état de la liaison et réglages courants.

const DEFAULTS = { autoCapture: true, removeAfterImport: false };
const $ = (id) => document.getElementById(id);

function show(level, state, detail) {
  $("dot").className = "dot " + level;
  $("state").textContent = state;
  $("detail").textContent = detail || "";
}

function describe(result) {
  if (!result) {
    return show("err", "Extension en erreur", "Rechargez l'extension depuis chrome://extensions.");
  }
  if (result.ok && result.transport === "native") {
    return result.running
      ? show("ok", "Application connectée", `Torrent Uploader ${result.version || ""} est ouvert.`)
      : show("ok", "Prête", "L'application est fermée : elle se lancera toute seule au premier envoi.");
  }
  if (result.ok && result.transport === "http") {
    return show("ok", "Application connectée", "Connexion manuelle (l'application doit rester ouverte).");
  }
  if (result.transport === "http") {
    return show("warn", "Application fermée",
      "Connexion manuelle : ouvrez Torrent Uploader pour recevoir les envois.");
  }
  return show("err", "Liaison non activée",
    "Dans Torrent Uploader : bouton « Intégration », puis « Activer ». Aucun autre réglage n'est nécessaire.");
}

async function refresh() {
  try {
    describe(await chrome.runtime.sendMessage({ type: "status" }));
  } catch (_) {
    describe(null);
  }
  const { lastResult } = await chrome.storage.session.get("lastResult").catch(() => ({}));
  if (lastResult && Date.now() - lastResult.at < 10 * 60 * 1000) {
    $("last").textContent = "Dernier envoi : " + lastResult.message;
    $("last").className = "msg " + (lastResult.ok ? "ok" : "err");
  }
}

async function load() {
  const cfg = await chrome.storage.local.get(DEFAULTS);
  for (const key of Object.keys(DEFAULTS)) {
    $(key).checked = !!cfg[key];
    $(key).addEventListener("change", () => chrome.storage.local.set({ [key]: $(key).checked }));
  }
  $("open").addEventListener("click", async () => {
    $("open").disabled = true;
    show("", "Ouverture…", "");
    try {
      await chrome.runtime.sendMessage({ type: "show" });
    } catch (_) { /* l'état ci-dessous dira ce qu'il en est */ }
    $("open").disabled = false;
    refresh();
  });
  $("options").addEventListener("click", (event) => {
    event.preventDefault();
    chrome.runtime.openOptionsPage();
  });
  refresh();
}

load();
