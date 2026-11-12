// Client CDP minimal par tube (--remote-debugging-pipe), sans dépendance.
// Sert aux tests de bout en bout de l'extension dans un vrai Chrome (profil temporaire).
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { existsSync } from 'node:fs';

/** Chrome installé : variable CHROME_PATH, sinon emplacements habituels sous Windows. */
export function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    `${process.env.LOCALAPPDATA || ''}/Google/Chrome/Application/chrome.exe`,
  ].filter(Boolean);
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error('Chrome introuvable : définissez CHROME_PATH');
  return found;
}

export async function launch({ headless = true, extraArgs = [] } = {}) {
  const profile = mkdtempSync(join(tmpdir(), 'tu-e2e-'));
  const args = [
    '--remote-debugging-pipe',
    '--enable-unsafe-extension-debugging',
    `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-search-engine-choice-screen',
    '--disable-features=DialMediaRouteProvider',
    ...(headless ? ['--headless=new'] : ['--window-size=900,700', '--window-position=40,40']),
    ...extraArgs,
    'about:blank',
  ];
  const child = spawn(findChrome(), args, { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] });
  const w = child.stdio[3], r = child.stdio[4];
  let nextId = 1, buf = '';
  const pending = new Map();
  const listeners = new Set();
  r.setEncoding('utf8');
  r.on('data', (chunk) => {
    buf += chunk;
    let i;
    while ((i = buf.indexOf('\0')) >= 0) {
      const raw = buf.slice(0, i); buf = buf.slice(i + 1);
      if (!raw) continue;
      const msg = JSON.parse(raw);
      if (msg.id && pending.has(msg.id)) {
        const { resolve, reject } = pending.get(msg.id); pending.delete(msg.id);
        msg.error ? reject(new Error(`${msg.error.message} ${msg.error.data || ''}`)) : resolve(msg.result);
      } else {
        for (const l of listeners) l(msg);
      }
    }
  });
  function send(method, params = {}, sessionId) {
    const id = nextId++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      w.write(JSON.stringify(payload) + '\0');
      setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error('timeout ' + method)); } }, 30000);
    });
  }
  async function close() {
    try { await send('Browser.close'); } catch (_) {}
    await new Promise((res) => { child.once('exit', res); setTimeout(res, 4000); });
    try { child.kill(); } catch (_) {}
    for (let k = 0; k < 5; k++) {
      try { rmSync(profile, { recursive: true, force: true }); break; } catch (_) { await sleep(500); }
    }
  }
  return { send, close, on: (l) => listeners.add(l), off: (l) => listeners.delete(l), profile, child };
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Attend le service worker de l'extension et s'y attache. */
export async function attachServiceWorker(browser, extId, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const { targetInfos } = await browser.send('Target.getTargets');
    const sw = targetInfos.find((t) => t.type === 'service_worker' && t.url.startsWith(`chrome-extension://${extId}/`));
    if (sw) {
      const { sessionId } = await browser.send('Target.attachToTarget', { targetId: sw.targetId, flatten: true });
      return sessionId;
    }
    await sleep(300);
  }
  throw new Error('service worker introuvable');
}

/** Evalue une expression (async autorisee) dans une session et retourne la valeur. */
export async function evaluate(browser, sessionId, expression) {
  const res = await browser.send('Runtime.evaluate', {
    expression: `(async () => { ${expression} })()`, awaitPromise: true, returnByValue: true,
  }, sessionId);
  if (res.exceptionDetails) {
    throw new Error('exception: ' + (res.exceptionDetails.exception?.description || res.exceptionDetails.text));
  }
  return res.result.value;
}
