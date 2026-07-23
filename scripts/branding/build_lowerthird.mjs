// Renders a corrected lower-third card (KIPPO [FU] · DM3) matching the baked style,
// at the same position, on a transparent 1920x1080 canvas.
import fs from 'node:fs';
const dir = process.argv[2];
const b64 = f => fs.readFileSync(`${dir}/${f}`).toString('base64');
const night = b64('qrf_Nightrunner-700-normal.woff2');
const mono  = b64('qrf_ShareTechMono-400-normal.woff2');

const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@font-face{font-family:'Nightrunner';src:url(data:font/ttf;base64,${night}) format('truetype');font-weight:700}
@font-face{font-family:'Share Tech Mono';src:url(data:font/woff2;base64,${mono}) format('woff2')}
*{margin:0;padding:0}html,body{width:1920px;height:1080px;background:transparent;overflow:hidden}
</style></head><body><canvas id="c" width="1920" height="1080"></canvas><script>
const g=document.getElementById('c').getContext('2d');
// card geometry (covers the baked ZAMSHA card x48..410,y944..1035 with margin)
const X=44,Y=940,Wd=388,Hd=102,R=8;
function rr(x,y,w,h,r){g.beginPath();g.moveTo(x+r,y);g.arcTo(x+w,y,x+w,y+h,r);g.arcTo(x+w,y+h,x,y+h,r);g.arcTo(x,y+h,x,y,r);g.arcTo(x,y,x+w,y,r);g.closePath();}
function draw(){
  g.clearRect(0,0,1920,1080);
  // panel
  rr(X,Y,Wd,Hd,R); g.fillStyle='rgba(8,16,38,0.96)'; g.fill();
  rr(X,Y,Wd,Hd,R); g.lineWidth=2; g.strokeStyle='rgba(229,29,42,0.85)'; g.stroke();
  // red left accent bar
  g.fillStyle='#e51d2a'; g.fillRect(X+6,Y+10,6,Hd-20);
  // label
  g.textBaseline='alphabetic';
  g.font="26px 'Share Tech Mono'"; g.fillStyle='#25d4ff'; g.letterSpacing='6px';
  g.fillText('NOW SHOWING · DEMO', X+26, Y+34);
  g.letterSpacing='0px';
  // big title: KIPPO [FU] · DM3 — auto-fit to card width
  const maxW=Wd-44; let size=54;
  function widthAt(s){
    g.font="700 "+s+"px 'Nightrunner'";
    return g.measureText('KIPPO ').width + (s*0.78) /*[FU] approx*/;
  }
  // measure precisely by composing parts
  function parts(s){
    g.font="700 "+s+"px 'Nightrunner'"; const w1=g.measureText('KIPPO ').width;
    const sub=Math.round(s*0.74); g.font="700 "+sub+"px 'Nightrunner'"; const w2=g.measureText('[FU] ').width;
    g.font="700 "+s+"px 'Nightrunner'"; const w3=g.measureText('· DM3').width;
    return {w1,w2,w3,sub,total:w1+w2+w3};
  }
  let p=parts(size);
  while(p.total>maxW && size>30){ size-=2; p=parts(size); }
  const by=Y+86; let x=X+26;
  g.textBaseline='alphabetic';
  g.shadowColor='rgba(0,0,0,0.6)'; g.shadowBlur=6;
  g.font="700 "+size+"px 'Nightrunner'"; g.fillStyle='#ffffff'; g.fillText('KIPPO ',x,by); x+=p.w1;
  g.font="700 "+p.sub+"px 'Nightrunner'"; g.fillStyle='#25d4ff'; g.fillText('[FU] ',x,by); x+=p.w2;
  g.font="700 "+size+"px 'Nightrunner'"; g.fillStyle='#ffffff'; g.fillText('· DM3',x,by);
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
fs.writeFileSync(`${dir}/lowerthird.html`, html);
console.log('wrote lowerthird.html');
