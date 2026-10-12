// Real query waiting, stale replies, keyboard input and missing-history display.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag='',className='',text='查询账号') {this.tag=tag;this.className=className;this.textContent=text;this.children=[];this.listeners={};this.attributes={};this.hidden=true;this.disabled=false;this.classList={toggle(){}};}
  setAttribute(name, value) {this.attributes[name]=value;}
  addEventListener(name, callback) {this.listeners[name]=callback;}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
}
function deferred() {let resolve, reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
const sandbox=vm.createContext({});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../web/launcher/account-query.js'),'utf8')+'\nglobalThis.factory=createAccountQuery',sandbox);
async function check() {
  const names=['input','button','result','summary','preset','cards'];
  const nodes=Object.fromEntries(names.map(n=>[n,new Element()]));
  let index=1048;const reads=[];
  const flow=sandbox.factory({...nodes,el:(...args)=>new Element(...args),getIndex:()=>index,api:path=>{const d=deferred();reads.push({path,...d});return d.promise;}});
  const old=flow.query();
  assert.equal(nodes.button.disabled,true);assert.match(nodes.summary.textContent,/1048/);
  assert.equal(nodes.result.attributes['aria-busy'],'true');
  index=1952;nodes.input.listeners.input();
  assert.equal(nodes.result.hidden,true);
  const current=flow.query();
  reads[0].resolve({index:1048,historical_preset:{name:'过时响应',status:'recorded'}});await old;
  assert.match(nodes.summary.textContent,/1952/);assert.equal(nodes.button.disabled,true);
  const owned=[{key:'member:1',kind:'member',name:'角色 A',title:'角色卡面',count:10,image:'/api/launcher/account-art/member/1'},
    {key:'support:1',kind:'support',name:'支援 A',title:'支援卡面',count:3,image:'/api/launcher/account-art/support/1'}];
  reads[1].resolve({index:1952,used:false,pull_count:62,ssr_total:13,cards:owned,historical_preset:{name:'mygo + mujica 60 抽',status:'recorded',planned_pulls:62}});await current;
  assert.match(nodes.preset.textContent,/mygo \+ mujica/);assert.match(nodes.preset.textContent,/保存计划 62 抽/);
  assert.equal(nodes.button.disabled,false);assert.equal(nodes.result.attributes['aria-busy'],'false');
  assert.equal(nodes.cards.hidden,false);assert.equal(nodes.cards.children.length,2);
  assert.equal(nodes.cards.children[0].children[0].textContent,'SSR 角色卡 · 10 张');
  assert.equal(nodes.cards.children[1].children[0].textContent,'SSR 支援卡 · 3 张');
  assert.equal(nodes.cards.children[1].children[1].children[0].children[0].children[0].src,'/api/launcher/account-art/support/1');
  assert.equal(nodes.cards.children[0].children[1].children[0].children[0].children[1].textContent,'×10');
  await flow.ensure();assert.equal(reads.length,2);
  const missing=flow.query();assert.equal(nodes.cards.hidden,true);assert.equal(nodes.cards.children.length,0);
  reads[2].resolve({index:1952,ssr_total:0,cards:[],historical_preset:{name:null,status:'not_recorded'}});await missing;
  assert.equal(nodes.cards.children[0].textContent,'此账号没有 SSR 卡牌。');
  assert.match(nodes.preset.textContent,/未记录历史预设/);assert.doesNotMatch(nodes.preset.textContent,/mygo/);
  const broken=flow.query();reads[3].reject(new Error('Not Found'));await broken;
  assert.match(nodes.summary.textContent,/重新打开控制台/);assert.equal(nodes.button.disabled,false);
  assert.equal(nodes.cards.hidden,true);assert.equal(nodes.cards.children.length,0);
  const unreadable=flow.query();reads[4].resolve({index:1952,ssr_total:0,cards:[],historical_preset:{status:'unavailable'}});await unreadable;
  assert.match(nodes.preset.textContent,/暂时不可读/);
  let prevented=false;nodes.input.listeners.keydown({key:'Enter',preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(reads[5].path,'accounts/1952');
  reads[5].resolve({index:1048});await new Promise(resolve=>setImmediate(resolve));
  assert.match(nodes.summary.textContent,/编号不一致/);
  assert.equal(nodes.cards.hidden,true);
  const noHistory=flow.query();reads[6].resolve({index:1952,ssr_total:0,cards:[],historical_preset:{status:'not_recorded'},pool_keys:['internal-pool'],draw_labels:['internal-label']});await noHistory;
  assert.doesNotMatch(nodes.preset.textContent,/历史卡池|internal-pool|internal-label/);
  console.log('Account query client checks passed');
}
check().catch(error=>{console.error(error);process.exitCode=1;});
