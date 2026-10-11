// Exercise the real quota UI with fake local responses; never contact Clash or a device.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ournotes/proxy/ui/static/app.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const elements = new Map(), calls = [];
function element() {
  return {textContent:'',disabled:false,children:[],attributes:{},
    classList:{toggle(){},add(){},remove(){}},addEventListener(name,fn){this[name]=fn;},
    setAttribute(name,value){this.attributes[name]=value;},
    appendChild(child){this.children.push(child);},
    replaceChildren(fragment){this.children=fragment.children;},
  };
}
const el = id => {if(!elements.has(id)) elements.set(id,element());return elements.get(id);};
const summaries=[element()], connectionBadges=[element()], rotationBadges=[element()];
let data = {connection:{state:'connected',node:'B',group:'Proxies',transport:'本机 HTTP',message:'已连接',checked_at:1},
  rotation:{protocol:2,state:'ready',threshold:150,used:4,reserved:2,remaining_nodes:1,next_node:'C',batch_id:'old',processed:900,batch_state:'complete',
    nodes:[{name:'B',used:4,reserved:2,remaining:144,current:true},{name:'C<img>',used:0,reserved:0,remaining:150}],message:'额度可用'}};
vm.runInNewContext(source, {
  document:{getElementById:el,querySelector:()=>({content:'token'}),hidden:false,
    querySelectorAll(selector){return selector.includes('summary')?summaries:selector.includes('connection')?connectionBadges:rotationBadges;},
    createDocumentFragment:element,createElement:element,addEventListener(){}},
  window:{consoleState:{subscribe(fn){fn({clash:data});},refresh:async()=>{}}},AbortSignal:{timeout(){}},Date,String,Math,
  setTimeout(){return 1;},clearTimeout(){},
  fetch:async (url, options)=>{calls.push({url,options});return {ok:true,json:async()=>data};},
});
(async()=>{
  await tick();
  assert.equal(el('clash-next').textContent,'C（候选）');
  assert.equal(el('clash-count').textContent,'4 / 150');
  assert.match(el('clash-countdown').textContent,/预留 2 次.*额度 144 次.*900.*本批已结束/);
  assert.equal(el('clash-nodes').children[1].children[0].textContent,'C<img>');
  assert.ok(!el('clash-message').textContent.includes('仍按批次顺序'));
  data = {...data,rotation:{...data.rotation,state:'draining',used:152,reserved:0,earliest_available_at:86400,
    last_switch:{node:'B',at:1},switch_count:1,message:'等待当前任务结束后切换'}};
  await el('clash-check').click();await tick();
  assert.equal(el('clash-count').textContent,'152 / 150');
  assert.equal(el('clash-progress').value,150);
  assert.match(el('clash-expiry').textContent,/北京时间.*不会自动恢复/);
  assert.match(el('clash-last').textContent,/累计切换 1 次/);
  assert.equal(calls.length,1);
  assert.equal(calls[0].options.method,'POST');
  assert.equal(calls[0].options.headers['X-Launcher-Token'],'token');
  assert.ok(calls.every(call=>/\/clash\/(status|check)$/.test(call.url)));
  data={...data,connection:{state:'unavailable',message:'不可用'}};
  await el('clash-check').click();await tick();
  assert.equal(el('clash-node').textContent,'未确认');
  assert.equal(el('clash-next').textContent,'连接确认后重新计算');
  data={...data,rotation:{processed:900,next_node:'old-fixed-target'}};
  await el('clash-check').click();await tick();
  assert.match(el('clash-message').textContent,/请重启控制台.*密码请先保存/);
  assert.equal(rotationBadges[0].textContent,'状态未更新');
  process.stdout.write('clash client checks passed\n');
})().catch(error=>{process.stderr.write(String(error.stack));process.exitCode=1;});
