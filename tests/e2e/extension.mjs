// Test de bout en bout de l'extension, dans un vrai Chrome (profil temporaire).
// Lancé par run_e2e.py, qui prépare l'application, l'hôte natif et les faux sites.
//
// Variables d'environnement :
//   EXT_DIR      dossier de l'extension
//   EXT_ID       identifiant attendu (fixé par la clé du manifeste)
//   TRK1, TRK2   origines des faux trackers (TRK2 hors des permissions de l'extension)
//   SCENARIO     "running" (application ouverte) ou "closed" (application fermée)
//   DL_DIR       dossier de téléchargement de Chrome
//   OUT          fichier JSON où écrire les résultats

import { existsSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { launch, attachServiceWorker, evaluate, sleep } from './cdp.mjs';

const { EXT_DIR, EXT_ID, TRK1, TRK2, SCENARIO, DL_DIR, OUT, SHOTS } = process.env;
const HEADLESS = !process.argv.includes('--headful');
const MAGNET = 'magnet:?xt=urn:btih:C12FE1C06BBA254A9DC9F519B335AA7C1367A88A&dn=Essai%20E2E';
const results = [];

function check(name, ok, detail = '') {
  results.push({ name, ok: !!ok, detail: typeof detail === 'string' ? detail : JSON.stringify(detail) });
  console.log(`${ok ? '[OK]   ' : '[ECHEC]'} ${name}${detail ? '  ' + (typeof detail === 'string' ? detail : JSON.stringify(detail)) : ''}`);
}

const browser = await launch({ headless: HEADLESS });
let sw, page;

async function lastResult() {
  return evaluate(browser, sw, `return (await chrome.storage.session.get('lastResult')).lastResult || null;`);
}

/** Attend un nouveau compte rendu d'envoi (posé par le service worker). */
async function waitResult(since, timeoutMs = 20000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const r = await lastResult();
    if (r && r.at > since) return r;
    await sleep(250);
  }
  return null;
}

async function navigate(url) {
  await browser.send('Page.navigate', { url }, page).catch(() => {});
  await sleep(1200);
}

/** Capture d'une page de l'extension à la largeur voulue (pour la notice). */
async function shoot(url, width, file) {
  await browser.send('Emulation.setEmulatedMedia', {
    features: [{ name: 'prefers-color-scheme', value: 'light' }] }, page);
  await browser.send('Emulation.setDeviceMetricsOverride',
    { width, height: 600, deviceScaleFactor: 2, mobile: false }, page);
  await navigate(url);
  await sleep(1800);
  const { result } = await browser.send('Runtime.evaluate', {
    expression: 'Math.ceil(document.body.getBoundingClientRect().bottom + 6)', returnByValue: true }, page);
  await browser.send('Emulation.setDeviceMetricsOverride',
    { width, height: result.value, deviceScaleFactor: 2, mobile: false }, page);
  await sleep(400);
  const { data } = await browser.send('Page.captureScreenshot', { format: 'png' }, page);
  writeFileSync(file, Buffer.from(data, 'base64'));
  await browser.send('Emulation.clearDeviceMetricsOverride', {}, page);
}

async function swNow() {
  return evaluate(browser, sw, 'return Date.now();');
}

try {
  // ---- chargement ---------------------------------------------------------
  const { id } = await browser.send('Extensions.loadUnpacked', { path: EXT_DIR });
  check("identifiant fixé par la clé du manifeste", id === EXT_ID, id);
  sw = await attachServiceWorker(browser, id);
  await sleep(1000);

  const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
  ({ sessionId: page } = await browser.send('Target.attachToTarget', { targetId, flatten: true }));
  await browser.send('Page.enable', {}, page);
  await browser.send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: DL_DIR, eventsEnabled: true });

  if (SCENARIO === 'running') {
    // ---- 1. liaison native ------------------------------------------------
    const ping = await evaluate(browser, sw,
      `try { return await chrome.runtime.sendNativeMessage('com.torrentuploader.host', {action: 'ping'}); }
       catch (e) { return {error: String(e.message || e)}; }`);
    check('messagerie native : hôte joint, application vue ouverte', ping && ping.ok && ping.running, ping);

    // ---- 2. capture d'un téléchargement (fichier laissé en place) ---------
    let t0 = await swNow();
    await navigate(`${TRK1}/dl/capture-simple.torrent`);
    let r = await waitResult(t0);
    check('téléchargement capturé et transmis', r && r.ok, r);
    check('fichier laissé dans le dossier de téléchargement',
      existsSync(join(DL_DIR, 'Capture Simple.torrent')), readdirSync(DL_DIR));

    // ---- 3. capture avec suppression du fichier ---------------------------
    await evaluate(browser, sw, `await chrome.storage.local.set({removeAfterImport: true}); return 1;`);
    t0 = await swNow();
    await navigate(`${TRK1}/dl/capture-supprimee.torrent`);
    r = await waitResult(t0);
    await sleep(1500);
    check('téléchargement capturé (mode suppression)', r && r.ok, r);
    check('fichier retiré du dossier de téléchargement',
      !existsSync(join(DL_DIR, 'Capture Supprimee.torrent')), readdirSync(DL_DIR));
    const still = await evaluate(browser, sw,
      `return (await chrome.downloads.search({filenameRegex: 'Capture Supprimee'})).length;`);
    check("entrée retirée de l'historique de Chrome", still === 0, String(still));
    await evaluate(browser, sw, `await chrome.storage.local.set({removeAfterImport: false}); return 1;`);

    // ---- 4. un téléchargement ordinaire n'est pas touché ------------------
    t0 = await swNow();
    await navigate(`${TRK1}/dl/document.pdf`);
    r = await waitResult(t0, 4000);
    check("un fichier qui n'est pas un .torrent est ignoré", r === null, r);

    // ---- 5. capture désactivée --------------------------------------------
    await evaluate(browser, sw, `await chrome.storage.local.set({autoCapture: false}); return 1;`);
    t0 = await swNow();
    await navigate(`${TRK1}/dl/non-capture.torrent`);
    r = await waitResult(t0, 4000);
    check('capture désactivée : rien envoyé', r === null, r);
    await evaluate(browser, sw, `await chrome.storage.local.set({autoCapture: true}); return 1;`);

    // ---- 6. clic droit sur un magnet ---------------------------------------
    t0 = await swNow();
    await evaluate(browser, sw,
      `chrome.contextMenus.onClicked.dispatch({menuItemId: 'tu-link', linkUrl: ${JSON.stringify(MAGNET)}}); return 1;`);
    r = await waitResult(t0);
    check('clic droit sur un lien magnet', r && r.ok, r);

    // ---- 7. clic droit, tracker privé, lecture dans la page (cookies) ------
    await navigate(`${TRK1}/login`);
    await navigate(`${TRK1}/page`);
    const tab1 = await evaluate(browser, sw, `const [t] = await chrome.tabs.query({active: true}); return t && {id: t.id};`);
    t0 = await swNow();
    await evaluate(browser, sw,
      `chrome.contextMenus.onClicked.dispatch({menuItemId: 'tu-link', frameId: 0, linkUrl: '${TRK1}/private/prive.torrent'}, ${JSON.stringify(tab1)}); return 1;`);
    r = await waitResult(t0);
    check('clic droit sur un .torrent protégé par session (lu dans la page)', r && r.ok, r);

    // ---- 8. clic droit, page hors permissions : repli par téléchargement ---
    await navigate(`${TRK2}/login`);
    await navigate(`${TRK2}/page`);
    await evaluate(browser, sw, `await chrome.storage.local.set({autoCapture: false}); return 1;`);
    const tab2 = await evaluate(browser, sw, `const [t] = await chrome.tabs.query({active: true}); return t && {id: t.id};`);
    t0 = await swNow();
    await evaluate(browser, sw,
      `chrome.contextMenus.onClicked.dispatch({menuItemId: 'tu-link', frameId: 0, linkUrl: '${TRK2}/private/repli.torrent'}, ${JSON.stringify(tab2)}); return 1;`);
    r = await waitResult(t0);
    check('repli : téléchargé par Chrome puis transmis (même capture désactivée)', r && r.ok, r);
    await evaluate(browser, sw, `await chrome.storage.local.set({autoCapture: true}); return 1;`);

    // ---- 9. clic droit sur un lien qui n'est pas un torrent ----------------
    await navigate(`${TRK1}/page`);
    t0 = await swNow();
    await evaluate(browser, sw,
      `chrome.contextMenus.onClicked.dispatch({menuItemId: 'tu-link', frameId: 0, linkUrl: '${TRK1}/page'}, ${JSON.stringify(tab1)}); return 1;`);
    r = await waitResult(t0);
    check("lien sans torrent : refus expliqué", r && !r.ok && /torrent/.test(r.message), r);

    // ---- 10. page d'options : diagnostic -----------------------------------
    await navigate(`chrome-extension://${id}/options.html`);
    await sleep(2500);
    const state = await browser.send('Runtime.evaluate', {
      expression: "document.getElementById('state').textContent", returnByValue: true }, page);
    check("page d'options : liaison automatique reconnue",
      /Liaison automatique active/.test(state.result.value), state.result.value);

    if (SHOTS) {
      t0 = await swNow();
      await evaluate(browser, sw,
        `chrome.contextMenus.onClicked.dispatch({menuItemId: 'tu-link', linkUrl: ${JSON.stringify(MAGNET)}}); return 1;`);
      await waitResult(t0);
      await shoot(`chrome-extension://${id}/popup.html`, 340, join(SHOTS, 'extension-fenetre.png'));
      await shoot(`chrome-extension://${id}/options.html`, 720, join(SHOTS, 'extension-options.png'));
      check('captures de la notice', existsSync(join(SHOTS, 'extension-options.png')));
    }
  } else {
    // ---- application fermée : l'hôte doit la lancer -----------------------
    const ping = await evaluate(browser, sw,
      `try { return await chrome.runtime.sendNativeMessage('com.torrentuploader.host', {action: 'ping'}); }
       catch (e) { return {error: String(e.message || e)}; }`);
    check("application fermée : l'hôte répond qu'elle ne tourne pas", ping && ping.ok && ping.running === false, ping);

    const t0 = await swNow();
    await evaluate(browser, sw,
      `chrome.contextMenus.onClicked.dispatch({menuItemId: 'tu-link', linkUrl: ${JSON.stringify(MAGNET)}}); return 1;`);
    const r = await waitResult(t0, 45000);
    check("magnet envoyé : l'application est lancée puis le reçoit",
      r && r.ok && /application lancée/.test(r.message), r);
  }
} catch (err) {
  check('exécution du scénario', false, String(err && err.stack || err));
} finally {
  writeFileSync(OUT, JSON.stringify(results, null, 2));
  await browser.close();
}
