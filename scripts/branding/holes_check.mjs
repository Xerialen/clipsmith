import { chromium } from 'playwright';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const htmlPath = process.argv[2], outDir = process.argv[3];
const url = pathToFileURL(htmlPath).href;
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await page.goto(url, { waitUntil: 'networkidle' });
await page.waitForFunction(() => {
  const t = document.getElementById('__bundler_thumbnail');
  return !t || getComputedStyle(t).display === 'none';
}, { timeout: 8000 }).catch(() => {});
await page.waitForTimeout(1200);

const base = path.basename(htmlPath).replace(/\.html$/i, '');

// Paint magenta behind everything; transparent areas of the design reveal it.
await page.addStyleTag({ content: 'html,body{background:#ff00ff !important;}' });
await page.screenshot({ path: path.join(outDir, base + ' [holes-magenta].png') });
console.log('done');
await browser.close();
