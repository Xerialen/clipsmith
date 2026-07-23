// Builds killcounter.html from a schedule.json (produced by logic.py via
// generate.py). Frame-driven like scripts/branding/build_intro.mjs:
// window.step() advances one frame; window.__f is the current frame index.
// Fonts are the OneMoreQUAD brand set, inlined base64 (Nightrunner is the
// gunzipped TTF; pick_fonts2.mjs already handled that).
//
// usage: node build_overlay.mjs <workdir>
//   reads  <workdir>/schedule.json + <workdir>/qrf_*.woff2|ttf fonts
//   writes <workdir>/killcounter.html
import fs from 'node:fs';
const dir = process.argv[2];
const schedule = fs.readFileSync(`${dir}/schedule.json`, 'utf8');
const b64 = f => fs.readFileSync(`${dir}/${f}`).toString('base64');
const FONT = {
  night:  b64('qrf_Nightrunner-700-normal.woff2'),   // actually gunzipped TTF
  chakraI: b64('qrf_ChakraPetch-700-italic.woff2'),
  chakra: b64('qrf_ChakraPetch-700-normal.woff2'),
};

const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@font-face{font-family:'Nightrunner';src:url(data:font/ttf;base64,${FONT.night}) format('truetype');font-weight:700}
@font-face{font-family:'Chakra Petch';font-style:italic;font-weight:700;src:url(data:font/woff2;base64,${FONT.chakraI}) format('woff2')}
@font-face{font-family:'Chakra Petch';font-weight:700;src:url(data:font/woff2;base64,${FONT.chakra}) format('woff2')}
*{margin:0;padding:0}html,body{background:transparent;overflow:hidden}
#c{display:block}
</style></head><body>
<canvas id="c"></canvas>
<script>
const S=${schedule};
const W=S.width,H=S.height,EV=S.events;
const CX=S.chip.cx, CY=S.chip.cy;
const cv=document.getElementById('c'); cv.width=W; cv.height=H;
const g=cv.getContext('2d');

// Brand palette (quad-runner-2026-05): deep navy + cyan; brand red for oops.
const NAVY='rgba(4,8,26,0.82)', NAVY2='rgba(10,24,56,0.82)';
const CYAN='#25d4ff', RED='#e51d2a';

function easeOut(t){return 1-Math.pow(1-t,3);}
function roundRect(ctx,x,y,w,h,r){
  ctx.beginPath();
  ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);
  ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath();
}

// Frame state: counter value + animations touching frame f.
function stateAt(f){
  let count=0, active=[];
  for(const e of EV){
    if(e.frame>f) break;
    if(e.type==='kill') count=e.count;
    if(f<=e.anim_end) active.push(e);
  }
  return {count, active};
}

window.__f=-1;
window.step=function(){
  const f=++window.__f;
  g.clearRect(0,0,W,H);
  if(!EV.length || f<EV[0].frame) return;           // nothing until first event
  const st=stateAt(f);

  // chip pops into existence with the first event
  const appear=Math.min(1,(f-EV[0].frame+1)/6);
  let popScale=1, ring=null, oops=null;
  for(const e of st.active){
    const t=Math.min(1,(f-e.frame)/Math.max(1,(e.anim_end-e.frame)));
    if(e.type==='kill'){ popScale=Math.max(popScale,1+0.85*Math.pow(1-t,2.4)); ring={t}; }
    else oops={t};
  }
  let rot=0, border=CYAN, glow='rgba(37,212,255,0.85)', label='KILLS', labelCol=CYAN;
  if(oops){                                          // comic wobble + red treatment
    rot=Math.sin(oops.t*Math.PI*5)*(1-oops.t)*0.14;
    border=RED; glow='rgba(229,29,42,0.9)'; labelCol=RED;
  }

  g.save(); g.globalAlpha=appear; g.translate(CX,CY); g.rotate(rot);
  // chip body
  const grd=g.createLinearGradient(0,-105,0,105);
  grd.addColorStop(0,NAVY); grd.addColorStop(1,NAVY2);
  roundRect(g,-150,-105,300,210,18);
  g.fillStyle=grd; g.fill();
  g.shadowColor=glow; g.shadowBlur=oops?34:22;
  g.lineWidth=4; g.strokeStyle=border; g.stroke();
  g.shadowBlur=0;
  // red flash fill on oops
  if(oops && oops.t<0.45){
    roundRect(g,-150,-105,300,210,18);
    g.fillStyle='rgba(229,29,42,'+(0.5*(1-oops.t/0.45))+')'; g.fill();
  }
  // label
  g.textAlign='center'; g.textBaseline='middle';
  g.font="700 30px 'Chakra Petch'"; g.letterSpacing='8px';
  g.fillStyle=labelCol; g.shadowColor=glow; g.shadowBlur=12;
  g.fillText(label,4,-62);
  g.shadowBlur=0; g.letterSpacing='0px';
  // the number (pop-scaled)
  g.save(); g.translate(0,28); g.scale(popScale,popScale);
  g.font="700 120px 'Nightrunner'";
  g.lineWidth=8; g.strokeStyle='rgba(4,8,26,0.95)';
  g.strokeText(String(st.count),0,0);
  g.shadowColor=glow; g.shadowBlur=28;
  g.fillStyle=oops?'#ffd7d7':'#ffffff';
  g.fillText(String(st.count),0,0);
  g.restore();
  g.restore();

  // expanding ring on kill pops (screen space, additive)
  if(ring){
    g.save(); g.globalCompositeOperation='lighter';
    g.strokeStyle='rgba(37,212,255,'+Math.max(0,(1-ring.t)*0.8)+')';
    g.lineWidth=10*(1-ring.t)+2;
    g.beginPath(); g.arc(CX,CY,130+ring.t*170,0,7); g.stroke();
    g.restore();
  }
  // OOPS! tag below the chip
  if(oops){
    const pop=Math.min(1,oops.t*4), fade=oops.t>0.75?(1-oops.t)/0.25:1;
    g.save(); g.globalAlpha=fade; g.translate(CX,CY+165);
    g.scale(0.6+0.4*easeOut(pop),0.6+0.4*easeOut(pop));
    g.rotate(-0.06+Math.sin(oops.t*Math.PI*4)*(1-oops.t)*0.1);
    g.textAlign='center'; g.textBaseline='middle';
    g.font="italic 700 56px 'Chakra Petch'";
    g.lineWidth=8; g.strokeStyle='rgba(4,8,26,0.95)'; g.strokeText('OOPS!',0,0);
    g.shadowColor='rgba(229,29,42,0.95)'; g.shadowBlur=24;
    g.fillStyle=RED; g.fillText('OOPS!',0,0);
    g.restore();
  }
};
// canvas does NOT lazy-load @font-face: force-load, then await readiness.
// Required brand fonts fail closed; screenshots are not trustworthy with
// fallback browser fonts.
const REQUIRED_FONTS=[
  "700 120px 'Nightrunner'",
  "700 30px 'Chakra Petch'",
  "italic 700 56px 'Chakra Petch'",
];
window.__ready=false;
window.__fontError=null;
async function boot(){
  try{
    const loaded=await Promise.all(REQUIRED_FONTS.map(async spec=>({spec,faces:await document.fonts.load(spec)})));
    await document.fonts.ready;
    const missing=Array.from(new Set(
      loaded.filter(({faces})=>!faces.length).map(({spec})=>spec)
        .concat(REQUIRED_FONTS.filter(spec=>!document.fonts.check(spec)))
    ));
    if(missing.length) throw new Error('required fonts not loaded: '+missing.join(', '));
    window.__ready=true;
  }catch(e){
    window.__fontError=(e&&e.message)?e.message:String(e);
  }
}
boot();
</script></body></html>`;
fs.writeFileSync(`${dir}/killcounter.html`, html);
console.log('wrote killcounter.html', html.length, 'bytes');
