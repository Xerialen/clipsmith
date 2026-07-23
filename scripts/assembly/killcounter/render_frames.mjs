// Steps killcounter.html one frame at a time and writes the alpha PNG
// sequence. Only frames listed in dynamic.json (animation active — computed
// by logic.dynamic_frames) are screenshotted; all other frames are
// pixel-identical to their predecessor and are copied, which turns ~1050
// screenshots into ~100.
//
// usage: node render_frames.mjs <workdir>
//   reads  <workdir>/killcounter.html, schedule.json, dynamic.json
//   writes <workdir>/frames/f%05d.png
import { chromium } from 'playwright';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';

const dir = process.argv[2];
const schedule = JSON.parse(fs.readFileSync(`${dir}/schedule.json`, 'utf8'));
const dynamic = new Set(JSON.parse(fs.readFileSync(`${dir}/dynamic.json`, 'utf8')));
const total = schedule.total_frames;
const outDir = `${dir}/frames`;
fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({
  viewport: { width: schedule.width, height: schedule.height },
  deviceScaleFactor: 1,
});
const errs = [];
page.on('pageerror', e => errs.push(e.message));
await page.goto(pathToFileURL(`${dir}/killcounter.html`).href, { waitUntil: 'load' });
await page.waitForFunction(() => window.__ready === true || window.__fontError, { timeout: 8000 })
  .catch(() => errs.push('fonts not ready'));
let fontError = null;
try {
  fontError = await page.evaluate(() => window.__fontError || null);
} catch (e) {
  errs.push(`failed to evaluate font error: ${e.message}`);
}
if (fontError) errs.push(`font load failed: ${fontError}`);
if (errs.length) {
  console.log(JSON.stringify({ errs, total, shot: 0, copied: 0 }));
  await browser.close();
  process.exit(1);
}

let prev = null, shot = 0, copied = 0;
for (let n = 0; n < total; n++) {
  await page.evaluate(() => window.step());
  const file = `${outDir}/f${String(n).padStart(5, '0')}.png`;
  if (dynamic.has(n) || prev === null) {
    await page.screenshot({ path: file, omitBackground: true });
    shot++;
  } else {
    fs.copyFileSync(prev, file);
    copied++;
  }
  prev = file;
}
console.log(JSON.stringify({ errs, total, shot, copied }));
await browser.close();
process.exit(errs.length ? 1 : 0);
