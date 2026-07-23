import fs from 'node:fs'; import { PNG } from 'pngjs';
const png=PNG.sync.read(fs.readFileSync(process.argv[2]));
const {width,height,data}=png; let minX=width,minY=height,maxX=0,maxY=0,f=false;
for(let y=0;y<height;y++)for(let x=0;x<width;x++){const a=data[(y*width+x)*4+3];
 if(a<16){f=true;if(x<minX)minX=x;if(x>maxX)maxX=x;if(y<minY)minY=y;if(y>maxY)maxY=y;}}
console.log(JSON.stringify(f?{x:minX,y:minY,w:maxX-minX+1,h:maxY-minY+1}:null));
