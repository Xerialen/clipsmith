import { chromium } from 'playwright'; import { pathToFileURL } from 'node:url';
const b=await chromium.launch({headless:true});
const p=await b.newPage({viewport:{width:1920,height:1080}});
await p.goto(pathToFileURL(process.argv[2]).href,{waitUntil:'load'});
await p.waitForFunction(()=>window.__ready===true,{timeout:6000}).catch(()=>{});
await p.waitForTimeout(300);
await p.screenshot({path:process.argv[3],omitBackground:true});
await b.close(); console.log('rendered');
