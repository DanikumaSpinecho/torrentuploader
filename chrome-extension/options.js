// Page d'options : comportement de la capture et connexion manuelle de secours.

const DEFAULTS = { autoCapture: true, removeAfterImport: false, notify: true, token: "", port: 38471 };
const TOGGLES = ["autoCapture", "removeAfterImport", "notify"];
const $ = (id) => document.getElementById(id);

function show(level, state, detail) {
  $("dot").className = "dot " + level;
  $("state").textContent = state;
  $("detail").textContent = detail || "";
}

async function diagnose() {
  show("", "Vérification…", "");
  let report;
  try {
    report = await chrome.runtime.sendMessage({ type: "diagnose" });
  } catch (_) {
    return show("err", "Extension en erreur", "Rechargez l'extension depuis chrome://extensions.");
  }
  const { native, http } = report || {};
  if (native && native.ok) {
    return show("ok", "Liaison automatique active",
      native.running
        ? `Torrent Uploader ${native.version || ""} est ouvert.`
        : "L'application est fermée : elle se lancera toute seule au premier envoi.");
  }
  if (http && http.ok) {
    return show("ok", "Connexion manuelle active", "L'application répond avec le jeton saisi.");
  }
  if (http && http.status === 403) {
    return show("err", "Jeton refusé", "Recopiez le jeton affiché par l'application.");
  }
  if (http) {
    return show("warn", "Application fermée",
      "Connexion manuelle : ouvrez Torrent Uploader. La liaison automatique n'est pas activée ("
      + ((native && native.error) || "hôte introuvable") + ").");
  }
  show("err", "Liaison non activée",
    "Activez l'intégration dans l'application (détail : " + ((native && native.error) || "hôte introuvable") + ").");
}

async function load() {
  const cfg = await chrome.storage.local.get(DEFAULTS);
  for (const key of TOGGLES) {
    $(key).checked = !!cfg[key];
    $(key).addEventListener("change", () => chrome.storage.local.set({ [key]: $(key).checked }));
  }
  $("token").value = cfg.token || "";
  $("port").value = cfg.port || DEFAULTS.port;
  $("test").addEventListener("click", diagnose);
  $("save").addEventListener("click", save);
  diagnose();
}

async function save() {
  const port = parseInt($("port").value, 10);
  const message = $("saved");
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    message.textContent = "Port invalide (1024 à 65535).";
    message.className = "msg err";
    return;
  }
  await chrome.storage.local.set({ token: $("token").value.trim(), port });
  message.textContent = "Enregistré.";
  message.className = "msg ok";
  diagnose();
}

load();
