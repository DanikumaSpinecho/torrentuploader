const DEFAULTS = { port: 38471, token: "", autoCapture: false };

const $ = (id) => document.getElementById(id);
const statusEl = $("status");

function say(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

async function load() {
  const cfg = await chrome.storage.local.get(DEFAULTS);
  $("token").value = cfg.token || "";
  $("port").value = cfg.port || DEFAULTS.port;
  $("autoCapture").checked = !!cfg.autoCapture;
  refreshPermState();
}

async function refreshPermState() {
  const granted = await chrome.permissions.contains({ origins: ["<all_urls>"] });
  $("permState").textContent = granted
    ? "Accès accordé."
    : "Accès non accordé — la capture automatique restera inactive.";
  $("grant").disabled = granted;
}

$("save").addEventListener("click", async () => {
  const port = parseInt($("port").value, 10);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    say("Port invalide.", "err");
    return;
  }
  const token = $("token").value.trim();
  if (!token) {
    say("Le jeton est obligatoire.", "err");
    return;
  }
  await chrome.storage.local.set({
    token,
    port,
    autoCapture: $("autoCapture").checked,
  });
  say("Enregistré.", "ok");
});

$("test").addEventListener("click", async () => {
  say("Test en cours…");
  const resp = await chrome.runtime.sendMessage({ type: "ping" });
  if (resp && resp.ok) {
    say(`Connecté à ${resp.data.app} ${resp.data.version}.`, "ok");
  } else if (resp && resp.status === 403) {
    say("Jeton refusé par l'application.", "err");
  } else {
    say(
      "Application injoignable : " +
        ((resp && (resp.error || "HTTP " + resp.status)) || "sans réponse") +
        ". Vérifiez qu'elle est ouverte.",
      "err"
    );
  }
});

$("grant").addEventListener("click", async () => {
  // Doit partir d'un geste de l'utilisateur, sinon Chrome refuse la demande.
  const granted = await chrome.permissions.request({ origins: ["<all_urls>"] });
  say(granted ? "Accès accordé." : "Accès refusé.", granted ? "ok" : "err");
  refreshPermState();
});

$("autoCapture").addEventListener("change", async () => {
  await chrome.storage.local.set({ autoCapture: $("autoCapture").checked });
  if ($("autoCapture").checked) refreshPermState();
});

load();
