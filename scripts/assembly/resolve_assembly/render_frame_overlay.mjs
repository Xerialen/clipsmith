// Render the `Demo Clip Focus` brand scene to an alpha PNG plus a patched
// lower-third card (player [CLAN] · MAP) — issue #25, reusing the proven
// scripts/branding techniques (render_template.mjs + build_lowerthird.mjs):
//   * the OBS scene is an HTML JS-bundler browser source: wait for the
//     bundler thumbnail to unmount, force transparent bg, omitBackground —
//     the video region is a genuine alpha hole;
//   * the scene's baked lower-third says "ZAMSHA · DM3" (encoded in the
//     bundle) so we COVER it with an opaque card at the same geometry;
//   * canvas does NOT lazy-load @font-face — await document.fonts.load()
//     before fillText or the first paint is the fallback font;
//   * fonts come pre-extracted (scripts/branding/pick_fonts2.mjs handles the
//     gzip'd-TTF Nightrunner gotcha) from --fonts dir.
//
// Usage:
//   node render_frame_overlay.mjs --scene "<scene>.html" --fonts <dir> \
//     --out-scene scene_alpha.png --out-lower lowerthird.png \
//     --player KIPPO --clan FU --map DM3
//
// The caller (assemble.py) composites scene_alpha + lowerthird with ffmpeg.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright';

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  if (i < 0 || i + 1 >= process.argv.length) {
    if (dflt !== undefined) return dflt;
    console.error(`missing --${name}`);
    process.exit(2);
  }
  return process.argv[i + 1];
}

const scenePath = arg('scene');
const fontsDir = arg('fonts');
const outScene = arg('out-scene');
const outLower = arg('out-lower');
const player = arg('player');
const clan = arg('clan', '');
const map = arg('map');

const b64 = f => fs.readFileSync(path.join(fontsDir, f)).toString('base64');

// ---------------------------------------------------------------- lower third
// Same card geometry/style as scripts/branding/build_lowerthird.mjs (covers
// the baked ZAMSHA card x48..410,y944..1035 with margin), text parameterized.
function lowerthirdHtml() {
  const night = b64('qrf_Nightrunner-700-normal.woff2');
  const mono = b64('qrf_ShareTechMono-400-normal.woff2');
  const cfg = JSON.stringify({ player, clan, map });
  return `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@font-face{font-family:'Nightrunner';src:url(data:font/ttf;base64,${night}) format('truetype');font-weight:700}
@font-face{font-family:'Share Tech Mono';src:url(data:font/woff2;base64,${mono}) format('woff2')}
*{margin:0;padding:0}html,body{width:1920px;height:1080px;background:transparent;overflow:hidden}
</style></head><body><canvas id="c" width="1920" height="1080"></canvas><script>
const CFG=${cfg};
const g=document.getElementById('c').getContext('2d');
const X=44,Y=940,Wd=388,Hd=102,R=8;
function rr(x,y,w,h,r){g.beginPath();g.moveTo(x+r,y);g.arcTo(x+w,y,x+w,y+h,r);g.arcTo(x+w,y+h,x,y+h,r);g.arcTo(x,y+h,x,y,r);g.arcTo(x,y,x+w,y,r);g.closePath();}
function draw(){
  g.clearRect(0,0,1920,1080);
  rr(X,Y,Wd,Hd,R); g.fillStyle='rgba(8,16,38,0.96)'; g.fill();
  rr(X,Y,Wd,Hd,R); g.lineWidth=2; g.strokeStyle='rgba(229,29,42,0.85)'; g.stroke();
  g.fillStyle='#e51d2a'; g.fillRect(X+6,Y+10,6,Hd-20);
  g.textBaseline='alphabetic';
  g.font="26px 'Share Tech Mono'"; g.fillStyle='#25d4ff'; g.letterSpacing='6px';
  g.fillText('NOW SHOWING · DEMO', X+26, Y+34);
  g.letterSpacing='0px';
  // big title: PLAYER [CLAN] · MAP — auto-fit to card width
  const name=CFG.player+' ', clanTxt=CFG.clan?('['+CFG.clan+'] '):'', tail='· '+CFG.map;
  const maxW=Wd-44; let size=54;
  function parts(s){
    g.font="700 "+s+"px 'Nightrunner'"; const w1=g.measureText(name).width;
    const sub=Math.round(s*0.74); g.font="700 "+sub+"px 'Nightrunner'";
    const w2=clanTxt?g.measureText(clanTxt).width:0;
    g.font="700 "+s+"px 'Nightrunner'"; const w3=g.measureText(tail).width;
    return {w1,w2,w3,sub,total:w1+w2+w3};
  }
  let p=parts(size);
  while(p.total>maxW && size>30){ size-=2; p=parts(size); }
  const by=Y+86; let x=X+26;
  g.shadowColor='rgba(0,0,0,0.6)'; g.shadowBlur=6;
  g.font="700 "+size+"px 'Nightrunner'"; g.fillStyle='#ffffff'; g.fillText(name,x,by); x+=p.w1;
  if(clanTxt){ g.font="700 "+p.sub+"px 'Nightrunner'"; g.fillStyle='#25d4ff'; g.fillText(clanTxt,x,by); x+=p.w2; }
  g.font="700 "+size+"px 'Nightrunner'"; g.fillStyle='#ffffff'; g.fillText(tail,x,by);
  g.shadowBlur=0;
  window.__ready=true;
}
async function go(){
  for(const spec of ["700 54px Nightrunner","700 40px Nightrunner","26px 'Share Tech Mono'"]){
    try{ await document.fonts.load(spec); }catch(e){}
  }
  try{ await document.fonts.ready; }catch(e){}
  draw();
}
go();
</script></body></html>`;
}

const browser = await chromium.launch({ headless: true });
try {
  // 1) Demo Clip Focus scene -> alpha PNG (render_template.mjs technique).
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  await page.goto(pathToFileURL(scenePath).href, { waitUntil: 'networkidle' });
  try {
    await page.waitForFunction(() => {
      const t = document.getElementById('__bundler_thumbnail');
      return !t || t.offsetParent === null || getComputedStyle(t).display === 'none';
    }, { timeout: 8000 });
  } catch { console.error('warn: bundler thumbnail did not unmount within 8s'); }
  await page.waitForTimeout(1500); // let CSS animation settle
  await page.addStyleTag({ content: 'html,body{background:transparent !important;}' });
  await page.screenshot({ path: outScene, omitBackground: true });
  await page.close();

  // 2) patched lower-third -> PNG.
  const lowerHtmlPath = outLower.replace(/\.png$/i, '.html');
  fs.writeFileSync(lowerHtmlPath, lowerthirdHtml());
  const p2 = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  await p2.goto(pathToFileURL(lowerHtmlPath).href, { waitUntil: 'load' });
  await p2.waitForFunction(() => window.__ready === true, { timeout: 6000 }).catch(() => {});
  await p2.waitForTimeout(300);
  await p2.screenshot({ path: outLower, omitBackground: true });
  await p2.close();
  console.log('rendered', outScene, outLower);
} finally {
  await browser.close();
}
