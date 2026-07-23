// Builds intro.html with brand fonts inlined as base64. Animation is frame-driven:
// window.step() advances one frame; window.__f is the current frame index.
import fs from 'node:fs';
const dir = process.argv[2];
const b64 = f => fs.readFileSync(`${dir}/${f}`).toString('base64');
const FONT = {
  night:   b64('qrf_Nightrunner-700-normal.woff2'),
  chakraI: b64('qrf_ChakraPetch-700-italic.woff2'),
  chakra:  b64('qrf_ChakraPetch-700-normal.woff2'),
  mono:    b64('qrf_ShareTechMono-400-normal.woff2'),
};

const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@font-face{font-family:'Nightrunner';src:url(data:font/ttf;base64,${FONT.night}) format('truetype');font-weight:700}
@font-face{font-family:'Chakra Petch';font-style:italic;font-weight:700;src:url(data:font/woff2;base64,${FONT.chakraI}) format('woff2')}
@font-face{font-family:'Chakra Petch';font-weight:700;src:url(data:font/woff2;base64,${FONT.chakra}) format('woff2')}
@font-face{font-family:'Share Tech Mono';src:url(data:font/woff2;base64,${FONT.mono}) format('woff2')}
*{margin:0;padding:0}html,body{width:1920px;height:1080px;background:transparent;overflow:hidden}
#c{display:block}
</style></head><body>
<canvas id="c" width="1920" height="1080"></canvas>
<script>
const TOTAL=150, FPS=30, W=1920, H=1080;
const cv=document.getElementById('c'), g=cv.getContext('2d');

// ---- Doom-fire heat buffer (low-res, scaled up) ----
const FW=240, FH=135;                       // fire grid
const fire=new Uint8Array(FW*FH);
const pal=[]; // fire palette 0..36 -> rgba
(function(){
  // navy->ember->gold->white ramp tuned to brand (red/gold)
  const stops=[[0,[4,8,26,0]],[6,[60,8,8,120]],[12,[150,24,12,200]],[18,[229,29,42,235]],
               [24,[255,120,30,245]],[30,[255,200,70,250]],[36,[255,245,210,255]]];
  for(let i=0;i<=36;i++){
    let a=stops[0],b=stops[stops.length-1];
    for(let s=0;s<stops.length-1;s++){if(i>=stops[s][0]&&i<=stops[s+1][0]){a=stops[s];b=stops[s+1];break;}}
    const t=(i-a[0])/((b[0]-a[0])||1);
    pal[i]=a[1].map((v,k)=>Math.round(v+(b[1][k]-v)*t));
  }
})();
const fcan=document.createElement('canvas');fcan.width=FW;fcan.height=FH;
const fctx=fcan.getContext('2d');const fimg=fctx.createImageData(FW,FH);
let burn=0; // global extra heat 0..1 for burn-away
let emit=0; // 0..1 strength of the behind-title fire emitter
function seedFire(){
  for(let x=0;x<FW;x++){ // bottom row hot
    fire[(FH-1)*FW+x]=Math.random()<0.9?36:20;
  }
  // emitter band behind the title so flames rise OVER the letters.
  // title center ~ row 57 (H*0.42); seed hot just below it across its width.
  if(emit>0){
    const row=78, x0=58, x1=FW-58;
    for(let y=row;y<row+5;y++)for(let x=x0;x<x1;x++){
      if(Math.random()<0.55*emit) fire[y*FW+x]=Math.max(fire[y*FW+x], Math.random()<0.7?36:26);
    }
  }
}
function stepFire(){
  for(let y=1;y<FH;y++)for(let x=0;x<FW;x++){
    const src=y*FW+x;
    const decay=(Math.random()*3)|0;
    const dst=src-FW+(((Math.random()*3)|0)-1);
    let v=fire[src]-decay+ (burn>0?Math.round(burn*2):0);
    if(v<0)v=0; if(v>36)v=36;
    const di=(dst>=0&&dst<FW*FH)?dst:src-FW;
    fire[di]=v;
  }
}
function drawFireTo(ctx,alphaMul){
  const d=fimg.data;
  for(let i=0;i<FW*FH;i++){const c=pal[fire[i]];const j=i*4;d[j]=c[0];d[j+1]=c[1];d[j+2]=c[2];d[j+3]=Math.round(c[3]*alphaMul);}
  fctx.putImageData(fimg,0,0);
  ctx.imageSmoothingEnabled=true;
  ctx.drawImage(fcan,0,0,FW,FH,0,0,W,H);
}

// ---- particles (ember burst on slam) ----
let parts=[];
function burst(n){for(let i=0;i<n;i++){const a=Math.random()*Math.PI*2,sp=6+Math.random()*26;
  parts.push({x:W/2,y:H/2,vx:Math.cos(a)*sp,vy:Math.sin(a)*sp-4,life:1,sz:2+Math.random()*5});}}
function stepParts(){parts.forEach(p=>{p.x+=p.vx;p.y+=p.vy;p.vy+=0.5;p.vx*=0.96;p.life-=0.022;});parts=parts.filter(p=>p.life>0);}
function drawParts(ctx){ctx.save();ctx.globalCompositeOperation='lighter';parts.forEach(p=>{
  const a=Math.max(0,p.life);ctx.fillStyle='rgba(255,'+(150+Math.random()*80|0)+',60,'+a+')';
  ctx.beginPath();ctx.arc(p.x,p.y,p.sz*p.life,0,7);ctx.fill();});ctx.restore();}

// ---- background grid (brand) ----
function bg(ctx,f){
  const grd=ctx.createLinearGradient(0,0,0,H);grd.addColorStop(0,'#04081a');grd.addColorStop(1,'#0a1838');
  ctx.fillStyle=grd;ctx.fillRect(0,0,W,H);
  ctx.save();ctx.strokeStyle='rgba(37,212,255,0.10)';ctx.lineWidth=2;
  const cx=W/2,hz=H*0.46;
  for(let i=-12;i<=12;i++){ctx.beginPath();ctx.moveTo(cx+i*60,hz);ctx.lineTo(cx+i*420,H);ctx.stroke();}
  for(let r=1;r<=10;r++){const y=hz+(H-hz)*(r*r/100);ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();}
  ctx.restore();
  // vignette
  const v=ctx.createRadialGradient(cx,H*0.46,200,cx,H*0.5,1200);v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,0.55)');
  ctx.fillStyle=v;ctx.fillRect(0,0,W,H);
}

// ---- title ----
function drawTitle(ctx,f){
  // slam timing: appears at f=4, slams 4->12, shake to 18, settle
  if(f<4) return;
  const t=Math.min(1,(f-4)/8);
  const ease=1-Math.pow(1-t,3);
  const scale=2.6-1.6*ease;                  // 2.6 -> 1.0
  let shake=0; if(f>=10&&f<22){shake=(22-f)/12*14;}
  const ox=(Math.random()*2-1)*shake, oy=(Math.random()*2-1)*shake;
  const cx=W/2+ox, cyName=H*0.42+oy;
  ctx.save();ctx.translate(cx,cyName);ctx.scale(scale,scale);ctx.textAlign='center';ctx.textBaseline='middle';
  // KIPPO — Nightrunner gold
  ctx.font="700 230px 'Nightrunner'";
  const grd=ctx.createLinearGradient(0,-120,0,120);
  grd.addColorStop(0,'#fff4c0');grd.addColorStop(0.45,'#ffd24a');grd.addColorStop(0.75,'#ff8a1e');grd.addColorStop(1,'#e51d2a');
  ctx.shadowColor='rgba(255,120,20,0.9)';ctx.shadowBlur=40;
  ctx.lineWidth=10;ctx.strokeStyle='rgba(40,6,6,0.9)';ctx.strokeText('KIPPO',0,0);
  ctx.fillStyle=grd;ctx.fillText('KIPPO',0,0);
  ctx.restore();
}
// clan suffix [ – FU – ] — drawn AFTER flames so it stays legible
function drawSuffix(ctx,f){
  if(f<8) return;
  const t2=Math.min(1,(f-8)/7);
  ctx.save();ctx.globalAlpha=t2*(burn>0?Math.max(0,1-burn*1.6):1);
  ctx.textAlign='center';ctx.textBaseline='middle';
  ctx.translate(W/2, H*0.42+150);
  ctx.font="italic 700 74px 'Chakra Petch'";ctx.letterSpacing='12px';
  ctx.lineWidth=8;ctx.strokeStyle='rgba(6,14,30,0.95)';ctx.strokeText('[ – FU – ]',0,0);
  ctx.shadowColor='rgba(37,212,255,0.9)';ctx.shadowBlur=22;
  ctx.fillStyle='#25d4ff';ctx.fillText('[ – FU – ]',0,0);
  ctx.restore();
}

// flash + shockwave ring on slam
let shock=null;
function flash(ctx,f){if(f>=10&&f<17){const a=(17-f)/7*0.95;ctx.save();ctx.globalCompositeOperation='lighter';ctx.fillStyle='rgba(255,225,170,'+a+')';ctx.fillRect(0,0,W,H);ctx.restore();}
  if(shock){ctx.save();ctx.globalCompositeOperation='lighter';ctx.strokeStyle='rgba(255,170,70,'+Math.max(0,shock.a)+')';ctx.lineWidth=18;ctx.beginPath();ctx.arc(W/2,H*0.42,shock.r,0,7);ctx.stroke();ctx.restore();}}

window.__f=-1;
window.step=function(){
  const f=++window.__f;
  // burn-away ramps in last 22 frames
  burn = f>=128 ? Math.min(1,(f-128)/20) : 0;
  emit = f>=8 ? Math.min(1,(f-8)/8) : 0;          // flames behind title
  seedFire(); for(let k=0;k<2;k++) stepFire();
  if(f===10){ burst(180); shock={r:0,a:1}; }
  stepParts(); if(shock){shock.r+=70;shock.a-=0.12;if(shock.a<=0)shock=null;}

  // compose onto an offscreen so we can erase (burn) to transparent
  g.clearRect(0,0,W,H);
  g.globalCompositeOperation='source-over';
  bg(g,f);
  // fire glow behind title (lighter)
  g.save();g.globalCompositeOperation='lighter';drawFireTo(g,0.55);g.restore();
  drawTitle(g,f);
  // flames licking up over the lower part of title
  g.save();g.globalCompositeOperation='lighter';g.globalAlpha=0.9;drawFireTo(g,0.5);g.restore();
  drawSuffix(g,f);
  drawParts(g);
  flash(g,f);

  // BURN AWAY: use hot fire as an erase mask over whole frame -> transparent reveal
  if(burn>0){
    // build erase mask: scale fire, threshold by burn
    const m=document.createElement('canvas');m.width=FW;m.height=FH;const mc=m.getContext('2d');
    const md=mc.createImageData(FW,FH);
    const thr=36-Math.round(burn*40); // lower threshold as burn grows -> erase more
    for(let i=0;i<FW*FH;i++){const hot=fire[i]>=thr?255:0;const j=i*4;md.data[j]=md.data[j+1]=md.data[j+2]=255;md.data[j+3]=hot;}
    mc.putImageData(md,0,0);
    g.save();g.globalCompositeOperation='destination-out';g.imageSmoothingEnabled=true;
    g.drawImage(m,0,0,FW,FH,0,0,W,H);g.restore();
    // ember rim glow at burn edges
    g.save();g.globalCompositeOperation='lighter';g.globalAlpha=burn;drawFireTo(g,0.6);g.restore();
  }
};
// prime a few fire steps so it's established at f0
for(let i=0;i<20;i++){seedFire();stepFire();}
document.fonts.ready.then(()=>{window.__ready=true;});
</script></body></html>`;
fs.writeFileSync(`${dir}/intro.html`, html);
console.log('wrote intro.html', html.length, 'bytes');
