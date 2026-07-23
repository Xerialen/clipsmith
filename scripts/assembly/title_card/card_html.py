"""Build the frame-driven title-card HTML (Playwright-rendered canvas).

Same technique as scripts/branding/build_intro.mjs (proven 2026-06-10):
fonts inlined as base64, a 1920x1080 canvas, and a deterministic
``window.step()`` that advances exactly one frame (``window.__f`` is the
current frame index). The renderer steps and screenshots — no wall-clock
animation, so frames are exact and the pulse peak lands on a known frame.

Canvas gotcha honored: ``document.fonts.load(...)`` is awaited for every
face/size before the first draw (canvas does NOT lazy-load @font-face).

Brand language (OneMoreQUAD / QUAD RUNNER kit): deep navy ramp
#04081a -> #14305f, perspective grid, cyan #25d4ff + red #e51d2a accents,
gold Nightrunner wordmark treatment.
"""

from __future__ import annotations

import json


def build_card_html(
    *,
    category: str,
    player: str,
    week: int,
    mode: str,  # "quad" | "pent" | "plain"
    total_frames: int,
    fps: int,
    pulse_peak_frame: int,
    font_face_css: str,
) -> str:
    params = {
        "category": category.upper(),
        "sub": "OF THE WEEK",
        "week": f"WEEK {week}",
        "player": player,
        "mode": mode,
        "total": total_frames,
        "fps": fps,
        "pulsePeakF": pulse_peak_frame,
        "pulse": mode in ("quad", "pent"),
    }
    return (
        HTML_TEMPLATE
        .replace("/*FONT_FACES*/", font_face_css)
        .replace("/*PARAMS*/", json.dumps(params))
    )


HTML_TEMPLATE = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
/*FONT_FACES*/
*{margin:0;padding:0}html,body{width:1920px;height:1080px;background:#04081a;overflow:hidden}
#c{display:block}
</style></head><body>
<canvas id="c" width="1920" height="1080"></canvas>
<script>
const P=/*PARAMS*/;
const W=1920,H=1080;
const cv=document.getElementById('c'),g=cv.getContext('2d');

// ---------- category styles ----------
const STYLES={
  quad:{ // quad = blue glow
    grad:['#e6f2ff','#9cc6ff','#3b7bff','#1742c8'],
    glow:'rgba(70,140,255,0.95)', boost:'rgba(140,190,255,',
    stroke:'rgba(6,12,40,0.95)', pentagram:false },
  pent:{ // pent = pentagram red
    grad:['#ffd9cf','#ff6a55','#e51d2a','#7a0a12'],
    glow:'rgba(229,29,42,0.95)', boost:'rgba(255,90,80,',
    stroke:'rgba(40,4,8,0.95)', pentagram:true },
  plain:{
    grad:['#f3f5fa','#dde3ee','#b9c2d4','#8d97ad'],
    glow:null, boost:null,
    stroke:'rgba(8,14,30,0.9)', pentagram:false }
};
const S=STYLES[P.mode]||STYLES.plain;

// ---------- easing / timing helpers (frame-driven, deterministic) ----------
const clamp01=t=>Math.max(0,Math.min(1,t));
const easeOut3=t=>1-Math.pow(1-t,3);
const ramp=(f,a,b)=>clamp01((f-a)/(b-a));
function pulseP(f){            // single pulse: 4-frame attack to peak, eased decay
  if(!P.pulse) return 0;
  const pk=P.pulsePeakF;
  if(f<pk-4||f>pk+18) return 0;
  if(f<=pk) return (f-(pk-4))/4;
  return Math.pow(1-(f-pk)/18,2);
}

// ---------- background: brand navy ramp + perspective grid + vignette ----------
function bg(f){
  const grd=g.createLinearGradient(0,0,0,H);
  grd.addColorStop(0,'#04081a');grd.addColorStop(1,'#14305f');
  g.fillStyle=grd;g.fillRect(0,0,W,H);
  g.save();g.strokeStyle='rgba(37,212,255,0.10)';g.lineWidth=2;
  const cx=W/2,hz=H*0.46;
  for(let i=-12;i<=12;i++){g.beginPath();g.moveTo(cx+i*60,hz);g.lineTo(cx+i*420,H);g.stroke();}
  for(let r=1;r<=10;r++){const y=hz+(H-hz)*(r*r/100);g.beginPath();g.moveTo(0,y);g.lineTo(W,y);g.stroke();}
  g.restore();
  // slow deterministic scan band (subtle life in the hold)
  const sy=(f*3.5)%(H+300)-150;
  const sg=g.createLinearGradient(0,sy-120,0,sy+120);
  sg.addColorStop(0,'rgba(37,212,255,0)');sg.addColorStop(0.5,'rgba(37,212,255,0.035)');sg.addColorStop(1,'rgba(37,212,255,0)');
  g.fillStyle=sg;g.fillRect(0,sy-120,W,240);
  const v=g.createRadialGradient(cx,H*0.46,200,cx,H*0.5,1200);
  v.addColorStop(0,'rgba(0,0,0,0)');v.addColorStop(1,'rgba(0,0,0,0.55)');
  g.fillStyle=v;g.fillRect(0,0,W,H);
}

// ---------- shrink-to-fit font sizing (long names / categories) ----------
function fitSize(text,base,maxW,tpl){
  let sz=base;
  for(;sz>30;sz-=4){g.font=tpl.replace('SZ',sz+'px');if(g.measureText(text).width<=maxW)break;}
  return sz;
}
let titleSize=null,playerSize=null;
function sizes(){
  if(titleSize===null) titleSize=fitSize(P.category,168,1640,"700 SZ 'Nightrunner'");
  if(playerSize===null) playerSize=fitSize(P.player,104,1460,"italic 700 SZ 'Chakra Petch'");
}

// ---------- week chip ----------
function drawWeek(f){
  const a=ramp(f,4,10); if(a<=0)return;
  g.save();g.globalAlpha=a;g.textAlign='center';g.textBaseline='middle';
  g.font="400 46px 'Share Tech Mono'";g.letterSpacing='14px';
  g.shadowColor='rgba(37,212,255,0.8)';g.shadowBlur=14;
  g.fillStyle='#25d4ff';
  g.fillText('—  '+P.week+'  —',W/2,272);
  g.restore();
}

// ---------- title: "<CATEGORY>" slam + "OF THE WEEK" ----------
function goldGrad(y0,y1){
  const grd=g.createLinearGradient(0,y0,0,y1);
  grd.addColorStop(0,'#fff4c0');grd.addColorStop(0.45,'#ffd24a');
  grd.addColorStop(0.75,'#ff8a1e');grd.addColorStop(1,'#e51d2a');
  return grd;
}
function drawTitle(f){
  const a=ramp(f,0,5); if(a<=0)return;
  const e=easeOut3(ramp(f,0,8));
  const scale=2.0-1.0*e;
  g.save();g.globalAlpha=a;g.translate(W/2,430);g.scale(scale,scale);
  g.textAlign='center';g.textBaseline='middle';
  g.font="700 "+titleSize+"px 'Nightrunner'";
  g.lineWidth=10;g.strokeStyle='rgba(40,12,6,0.9)';g.strokeText(P.category,0,0);
  g.shadowColor='rgba(255,140,30,0.85)';g.shadowBlur=36;
  g.fillStyle=goldGrad(-titleSize*0.55,titleSize*0.55);
  g.fillText(P.category,0,0);
  g.restore();
  // slam flash
  if(f>=7&&f<12){g.save();g.globalCompositeOperation='lighter';
    g.fillStyle='rgba(255,225,170,'+(0.22*(12-f)/5)+')';g.fillRect(0,0,W,H);g.restore();}
  // OF THE WEEK
  const a2=ramp(f,5,12); if(a2<=0)return;
  g.save();g.globalAlpha=a2;g.textAlign='center';g.textBaseline='middle';
  g.translate(W/2,575);
  g.font="700 64px 'Nightrunner'";g.letterSpacing='6px';
  g.lineWidth=6;g.strokeStyle='rgba(20,10,6,0.9)';g.strokeText(P.sub,0,0);
  g.shadowColor='rgba(255,170,60,0.7)';g.shadowBlur=20;
  g.fillStyle=goldGrad(-40,40);g.fillText(P.sub,0,0);
  g.restore();
}

// ---------- divider: cyan rules + red diamond ----------
function drawDivider(f){
  const t=easeOut3(ramp(f,9,16)); if(t<=0)return;
  const y=668,half=520*t;
  g.save();g.strokeStyle='rgba(37,212,255,0.85)';g.lineWidth=3;
  g.shadowColor='rgba(37,212,255,0.8)';g.shadowBlur=10;
  g.beginPath();g.moveTo(W/2-40,y);g.lineTo(W/2-40-half,y);g.stroke();
  g.beginPath();g.moveTo(W/2+40,y);g.lineTo(W/2+40+half,y);g.stroke();
  g.shadowColor='rgba(229,29,42,0.9)';g.shadowBlur=14;
  g.fillStyle='#e51d2a';g.translate(W/2,y);g.rotate(Math.PI/4);
  const s=13*t;g.fillRect(-s,-s,2*s,2*s);
  g.restore();
}

// ---------- pentagram (PENT RUNNER texture, behind the name) ----------
function drawPentagram(x,y,r,alpha,lw){
  g.save();g.globalAlpha=alpha;
  g.strokeStyle='#e51d2a';g.lineWidth=lw;
  g.shadowColor='rgba(229,29,42,0.9)';g.shadowBlur=26;
  g.beginPath();g.arc(x,y,r,0,Math.PI*2);g.stroke();
  // inverted 5-point star (point down)
  const pts=[];for(let i=0;i<5;i++){const a=Math.PI/2+i*2*Math.PI/5;pts.push([x+r*Math.cos(a),y+r*Math.sin(a)]);}
  g.beginPath();g.moveTo(pts[0][0],pts[0][1]);
  for(let i=0;i<=5;i++){const p=pts[(i*2)%5];g.lineTo(p[0],p[1]);}
  g.closePath();g.stroke();
  g.restore();
}

// ---------- player name (category-styled, single synced pulse) ----------
function drawName(f){
  const a=ramp(f,12,20); if(a<=0)return;
  const rise=(1-easeOut3(a))*26;
  const p=pulseP(f);
  const y=800+rise, scale=1+0.16*p;
  sizes();
  if(S.pentagram){
    const pa=a*(0.16+0.55*p);
    drawPentagram(W/2,y,205+30*p,pa,5+4*p);
  }
  g.save();g.globalAlpha=a;g.translate(W/2,y);g.scale(scale,scale);
  g.textAlign='center';g.textBaseline='middle';
  g.font="italic 700 "+playerSize+"px 'Chakra Petch'";
  g.lineWidth=8;g.strokeStyle=S.stroke;g.strokeText(P.player,0,0);
  if(S.glow){g.shadowColor=S.glow;g.shadowBlur=26+70*p;}
  const grd=g.createLinearGradient(0,-playerSize*0.6,0,playerSize*0.6);
  S.grad.forEach((c,i)=>grd.addColorStop(i/(S.grad.length-1),c));
  g.fillStyle=grd;g.fillText(P.player,0,0);
  if(S.glow){g.fillText(P.player,0,0);} // double pass deepens the glow
  // pulse boost: additive bloom pass, peaks exactly on the sound transient
  if(p>0&&S.boost){
    g.globalCompositeOperation='lighter';
    g.shadowBlur=90*p;g.fillStyle=S.boost+(0.55*p)+')';
    g.fillText(P.player,0,0);
  }
  g.restore();
}

// ---------- frame step (called by the renderer) ----------
window.__f=-1;
window.step=function(){
  const f=++window.__f;
  g.clearRect(0,0,W,H);
  bg(f);
  drawWeek(f);
  drawTitle(f);
  drawDivider(f);
  drawName(f);
};

// canvas does NOT lazy-load @font-face: await every face before first draw.
// Required brand fonts fail closed; screenshots are not trustworthy with
// fallback browser fonts.
const REQUIRED_FONTS=[
  "700 170px 'Nightrunner'",
  "700 64px 'Nightrunner'",
  "italic 700 104px 'Chakra Petch'",
  "700 104px 'Chakra Petch'",
  "400 46px 'Share Tech Mono'",
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
</script></body></html>
"""
