import { chromium } from 'playwright';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const htmlPath = process.argv[2];
const outDir = process.argv[3];
const url = pathToFileURL(htmlPath).href;

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({
  viewport: { width: 1920, height: 1080 },
  deviceScaleFactor: 1,
});
const page = await ctx.newPage();

const errors = [];
page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
page.on('pageerror', e => errors.push('pageerror: ' + e.message));

await page.goto(url, { waitUntil: 'networkidle' });

// The bundler shows "#__bundler_thumbnail" until the real design mounts.
// Wait for that placeholder to disappear (or 8s max).
try {
  await page.waitForFunction(() => {
    const t = document.getElementById('__bundler_thumbnail');
    return !t || t.offsetParent === null || getComputedStyle(t).display === 'none';
  }, { timeout: 8000 });
} catch { errors.push('warn: thumbnail did not unmount within 8s'); }

await page.waitForTimeout(1500); // let any CSS animation settle to a frame

const base = path.basename(htmlPath).replace(/\.html$/i, '');

// 1) Flat render (whatever background the page declares)
await page.screenshot({ path: path.join(outDir, base + ' [flat].png') });

// 2) Alpha render: force the page/body backgrounds transparent, screenshot with omitBackground.
await page.addStyleTag({ content: 'html,body{background:transparent !important;}' });
await page.screenshot({ path: path.join(outDir, base + ' [alpha].png'), omitBackground: true });

// Report what actually rendered: body's computed bg + top-level element summary
const info = await page.evaluate(() => {
  const bodyBg = getComputedStyle(document.body).backgroundColor;
  const kids = [...document.body.children]
    .filter(el => el.offsetParent !== null || el === document.body.firstElementChild)
    .map(el => ({ tag: el.tagName, id: el.id, cls: el.className?.toString().slice(0,40),
                  w: el.offsetWidth, h: el.offsetHeight }));
  return { bodyBg, kids };
});

console.log(JSON.stringify({ ok: true, errors, info }, null, 2));
await browser.close();
