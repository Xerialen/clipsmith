import { chromium } from 'playwright';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
const dir=process.argv[2], total=parseInt(process.argv[3]||'150'), only=process.argv[4]; // only="6,12,.." for preview
fs.mkdirSync(`${dir}/intro_frames`,{recursive:true});
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1920,height:1080},deviceScaleFactor:1});
const errs=[];page.on('pageerror',e=>errs.push(e.message));
await page.goto(pathToFileURL(`${dir}/intro.html`).href,{waitUntil:'load'});
await page.waitForFunction(()=>window.__ready===true,{timeout:8000}).catch(()=>errs.push('fonts not ready'));
const want = only?new Set(only.split(',').map(Number)):null;
for(let n=0;n<total;n++){
  await page.evaluate(()=>window.step());
  if(want && !want.has(n)) continue;
  await page.screenshot({path:`${dir}/intro_frames/f${String(n).padStart(3,'0')}.png`,omitBackground:true});
}
console.log(JSON.stringify({errs, rendered: want?[...want]:total}));
await browser.close();
