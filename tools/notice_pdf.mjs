// Convertit la notice HTML en PDF A4 avec Chrome (sans dépendance).
//   node tools/notice_pdf.mjs <notice.html> <sortie.pdf>
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { launch, sleep } from '../tests/e2e/cdp.mjs';

const [, , input, output] = process.argv;
if (!input || !output) {
  console.error('usage : node tools/notice_pdf.mjs <notice.html> <sortie.pdf>');
  process.exit(2);
}
readFileSync(input);                                   // échoue tôt si le fichier manque

const browser = await launch({ headless: true });
try {
  const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
  await browser.send('Page.enable', {}, sessionId);
  await browser.send('Emulation.setEmulatedMedia', {
    media: 'print', features: [{ name: 'prefers-color-scheme', value: 'light' }] }, sessionId);
  await browser.send('Page.navigate', { url: pathToFileURL(input).href }, sessionId);
  for (let i = 0; i < 40; i++) {                      // attend le chargement des images
    const { result } = await browser.send('Runtime.evaluate', {
      expression: "document.readyState === 'complete' && [...document.images].every(i => i.complete)",
      returnByValue: true }, sessionId);
    if (result.value) break;
    await sleep(250);
  }
  const { data } = await browser.send('Page.printToPDF', {
    printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true,
    headerTemplate: '<span></span>',
    footerTemplate: '<div style="width:100%;font:8px Segoe UI,sans-serif;color:#888;text-align:center">'
      + 'Torrent Uploader 2 — page <span class="pageNumber"></span> / <span class="totalPages"></span></div>',
  }, sessionId);
  writeFileSync(output, Buffer.from(data, 'base64'));
  console.log('notice PDF écrite :', output);
} finally {
  await browser.close();
}
