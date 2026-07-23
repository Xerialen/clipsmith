import fs from 'node:fs'; import zlib from 'node:zlib';
const html=fs.readFileSync(process.argv[2],'utf8'); const outDir=process.argv[3];
function grab(t){const o=`<script type="__bundler/${t}">`;const i=html.indexOf(o);if(i<0)return null;const j=html.indexOf('</script>',i+o.length);return html.slice(i+o.length,j);}
let css=grab('template'); try{const j=JSON.parse(css);css=typeof j==='string'?j:JSON.stringify(j);}catch{}
const manifest=JSON.parse(grab('manifest'));
const faces=css.match(/@font-face\s*\{[^}]*\}/g)||[]; const map={};
for(const f of faces){const fam=(f.match(/font-family:\s*['"]?([^;'"}]+)/)||[])[1];const uuid=(f.match(/[0-9a-f-]{36}/)||[])[0];const w=((f.match(/font-weight:\s*([^;}]+)/)||[])[1]||'400').trim();const s=((f.match(/font-style:\s*([^;}]+)/)||[])[1]||'normal').trim();if(fam&&uuid)map[`${fam}|${w}|${s}`]=uuid;}
function save(fam,w,s,fn){
  let key=`${fam}|${w}|${s}`; if(!map[key])key=Object.keys(map).find(k=>k.startsWith(fam+'|'));
  const uuid=map[key]; const e=manifest[uuid]; if(!e){console.log('MISS',fam);return;}
  let raw=Buffer.from((e.data||'').replace(/^data:[^,]*,/,''),'base64');
  if(e.compressed){ try{ raw=zlib.gunzipSync(raw);}catch(err){ try{raw=zlib.inflateSync(raw);}catch(e2){console.log('decompress fail',fam,err.message);} } }
  const magic=raw.slice(0,4).toString('latin1');
  fs.writeFileSync(`${outDir}/${fn}`, raw);
  console.log(`${fn}  compressed=${e.compressed}  bytes=${raw.length}  magic=${magic}`);
}
save('Nightrunner','700','normal','qrf_Nightrunner-700-normal.woff2');
save('Chakra Petch','700','italic','qrf_ChakraPetch-700-italic.woff2');
save('Chakra Petch','700','normal','qrf_ChakraPetch-700-normal.woff2');
save('Share Tech Mono','400','normal','qrf_ShareTechMono-400-normal.woff2');
