const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/launcher/app.js'), 'utf8').split('const $ =')[0];
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise, resolve, reject};}
class Element {
  constructor(text) {this.textContent=text; this.children=[]; this.attrs={}; this.classes=new Set(); this.listeners={}; this.classList={toggle:(k,v)=>v?this.classes.add(k):this.classes.delete(k)};}
  append(...nodes) {this.children.push(...nodes);}
  setAttribute(k,v) {this.attrs[k]=v;}
  addEventListener(k,v) {this.listeners[k]=v;}
}
const batch={id:'batch'}, lane={instance:4,run_id:'run',status:'paused',recovery:{version:'v',retry:{enabled:true},restart_game:{enabled:true}}};
function fixture(api, saved='[]') {
  let data=saved;
  const storage={getItem:()=>data, setItem:(k,v)=>{data=v;}};
  const context=vm.createContext({crypto:{randomUUID:()=> 'same-request'}, sessionStorage:storage});
  vm.runInContext(source+'\nglobalThis.factory=createLaneRecovery;',context);
  const messages=[];
  const flow=context.factory({api,storage,el:(tag,cls,text)=>new Element(text),onChange:()=>{},onMessage:m=>messages.push(m)});
  return {flow,messages,saved:()=>data};
}
(async()=>{
  const response=deferred(), receipt=deferred(), calls=[];
  const f=fixture((path,body)=>{calls.push({path,body});return path==='action'?response.promise:receipt.promise;});
  const submitting=f.flow.start(batch,lane,'restart_game');
  await f.flow.start(batch,lane,'retry');
  let buttons=f.flow.render(batch,lane).children[0].children;
  assert.equal(calls.length,1); assert(buttons.every(b=>b.disabled));
  assert.equal(buttons[1].attrs['aria-busy'],'true'); assert(buttons[1].classes.has('is-loading'));
  response.reject(new Error('lost response')); await tick();
  assert.equal(calls[1].path,'operations/same-request');
  receipt.resolve({status:'accepted'}); await submitting;
  assert(f.flow.render(batch,lane).children[0].children.every(b=>b.disabled));
  const queries=[];
  const reloaded=fixture(async (p,b)=>{queries.push({p,b});return {status:'succeeded',result:{message:'已接入'}};},f.saved());
  reloaded.flow.poll(batch); await tick();
  assert.equal(queries.length,1); assert.equal(queries[0].p,'operations/same-request');
  assert(reloaded.flow.render(batch,lane).children[0].children.every(b=>!b.disabled));
  assert.equal(reloaded.messages[0],'已接入');
  const blocked=structuredClone(lane); blocked.recovery.retry={enabled:false,reason:'未决交易'};
  const noCalls=[]; const g=fixture(async p=>{noCalls.push(p);});
  await g.flow.start(batch,blocked,'retry'); assert.equal(noCalls.length,0);
  assert(g.flow.render(batch,blocked).children[0].children[0].disabled);
  assert.equal(g.flow.render(batch,blocked).children[1].textContent,'未决交易');
  const uncertain=fixture(async p=>{throw new Error('offline');});
  await uncertain.flow.start(batch,lane,'retry');
  assert(uncertain.flow.render(batch,lane).children[0].children.every(b=>b.disabled));
  const before=uncertain.saved(); uncertain.flow.poll({id:'other-batch'}); await tick();
  assert.equal(uncertain.saved(),before);
  // A confirmed running lane retires stale persisted requests without another mutation.
  const running={...lane,status:'running'};
  assert.equal(uncertain.flow.render(batch,running),null);
  uncertain.flow.poll(batch,[lane]); await tick();
  assert.equal(uncertain.saved(),before); // Still paused: keep the uncertain receipt protected.
  uncertain.flow.poll(batch,[running]);
  assert.equal(uncertain.saved(),'[]');
  const restoredCalls=[];
  const restored=fixture(async p=>{restoredCalls.push(p);},before);
  restored.flow.poll(batch,[running]); await tick();
  assert.equal(restored.saved(),'[]'); assert.equal(restoredCalls.length,0);
  // A late receipt must not erase a newer recovery request or show an obsolete failure.
  const late=deferred();
  const racing=fixture(async ()=>late.promise,before);
  racing.flow.poll(batch,[lane]);
  racing.flow.poll(batch,[running]);
  late.resolve({status:'failed',result:{message:'obsolete error'}}); await tick();
  assert.equal(racing.saved(),'[]'); assert.equal(racing.messages.length,0);
  console.log('lane recovery client checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
