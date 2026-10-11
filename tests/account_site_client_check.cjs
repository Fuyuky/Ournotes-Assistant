// Standalone Node check: browser-side AND matching, pagination and snapshot refresh.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../web/account-browser/static-source.js"), "utf8");
const sandbox = {Set, Math, Number, Error};
vm.createContext(sandbox);
vm.runInContext(source + "\nglobalThis.source = accountSource;", sandbox);
const snapshot = {
  schema_version: 2, revision: "a".repeat(64),
  cards: [{key:"member:1"}, {key:"support:1"}], meta: {available:true},
  accounts: [
    {index:3, ssr_total:3, cards:[{key:"member:1",count:2},{key:"support:1",count:1}]},
    {index:1, ssr_total:3, cards:[{key:"member:1",count:3}]},
    {index:2, ssr_total:1, cards:[{key:"support:1",count:1}]},
    {index:4, ssr_total:0, cards:[]}
  ]
};
snapshot.accounts.forEach(a=>{a.price={status:'priced',amount_cents:1000};});
const options = {cards:"", order:"desc", page:1, page_size:10};
const ids = overrides => Array.from(sandbox.querySnapshot(snapshot, {...options,...overrides}).accounts, a=>a.index);
assert.deepEqual(ids({}), [1,3,2,4]);
assert.deepEqual(ids({order:"asc"}), [4,2,1,3]);
assert.deepEqual(ids({cards:"member:1"}), [1,3]);
assert.deepEqual(ids({cards:"support:1"}), [3,2]);
assert.deepEqual(ids({cards:"member:1,support:1"}), [3]);
assert.deepEqual(ids({cards:"member:1,member:1"}), [1,3]);
assert.deepEqual(ids({cards:"member:99"}), []);
assert.deepEqual(ids({page_size:2,page:2}), [2,4]);
assert.equal(sandbox.querySnapshot(snapshot, {...options,page:99}).page, 1);
assert.deepEqual(ids({page_size:2,page:99}), [2,4]);
snapshot.accounts.forEach((a,i)=>{a.used=i===0;a.quality=[4-i,i,a.ssr_total,1,i];a.price={status:"priced",amount_cents:(10+i)*100};});
assert.deepEqual(ids({}), [1,2,4]);
assert.deepEqual(ids({index:'3'}), [3], 'Exact index lookup includes used accounts');
assert.deepEqual(ids({index:'003',include_used:true}), [3]);
assert.deepEqual(ids({order:'price_desc',include_used:true}), [4,2,1,3]);
assert.deepEqual(ids({order:'price_asc',include_used:true}), [3,1,2,4]);
assert.deepEqual(ids({index:'2',cards:'member:1',include_used:true}), []);
assert.throws(()=>ids({index:'1.5'}), /invalid_index/);
assert.throws(()=>ids({index:'9007199254740992'}), /invalid_index/);
snapshot.accounts.forEach((a,i)=>{a.pull_count=i<2?43:i===2?62:null;});
assert.deepEqual(ids({pull_count:'43',include_used:true}),[1,3]);
assert.deepEqual(ids({pull_count:'62'}),[2]);
assert.deepEqual(ids({pull_count:'62',cards:'member:1'}),[]);
assert.deepEqual(ids({pull_count:'43',index:'2'}),[]);
assert.throws(()=>ids({pull_count:'44'}),/invalid_pull_count/);
snapshot.meta.public_account_limit = 300;
assert.deepEqual(ids({index:'3',include_used:true}), [3], 'Public and local used filtering match');
assert.deepEqual(ids({order:'price_desc',include_used:true}), [4,2,1,3]);
snapshot.accounts.forEach(a=>{a.price={status:a.price.status,amount_cents:a.price.amount_cents};});
assert.deepEqual(ids({order:'price_asc',include_used:true}), [3,1,2,4], 'Public price ordering');
snapshot.accounts[2].price={status:'pending',amount_cents:null};
assert.deepEqual(ids({order:'price_desc',include_used:true}),[4,1,3]);
assert.deepEqual(ids({index:'2'}),[2], 'Exact index lookup includes pending accounts');
assert.deepEqual(ids({index:'2',include_used:true}),[2]);
assert.deepEqual(ids({order:'price_desc',include_used:true,include_pending:true}),[2,4,1,3]);
assert.deepEqual(ids({order:'price_asc',include_used:true,include_pending:'true'}),[2,3,1,4]);
assert.deepEqual(ids({index:'2',include_pending:true,pull_count:'43'}),[]);
assert.deepEqual(ids({cards:'member:1',include_pending:true}),[1]);
snapshot.accounts[2].used=true;
assert.deepEqual(ids({index:'2',include_pending:true}),[2]);
assert.deepEqual(ids({index:'2',include_used:true}),[2], 'Used overrides pending visibility');
assert.deepEqual(ids({index:'2',include_used:'true',include_pending:false}),[2]);
assert.deepEqual(ids({index:'2',include_pending:true,include_used:true}),[2]);
snapshot.accounts[2].used=false;
snapshot.accounts[2].price={status:'priced',amount_cents:1200};
assert.deepEqual(ids({cards:'member:1,support:1',order:'selected_desc',include_used:true}),[3]);
assert.deepEqual(ids({cards:'member:1,support:1',order:'selected_desc',allow_missing:true,include_used:true}),[3,1,2], 'Equal copy counts prefer more distinct selected identities, before price');
assert.deepEqual(ids({cards:'member:1,member:1',order:'selected_desc',include_used:true}),[1,3]);
assert.deepEqual(ids({cards:'member:999',order:'selected_desc',allow_missing:true,include_used:true}),[]);
assert.throws(()=>ids({order:'selected_desc'}),/select_cards_first/);
assert.throws(()=>ids({order:'selected_any_desc'}),/invalid_order/);

snapshot.accounts.forEach((a,i)=>{a.mygo_characters=[2,1,0,0][i];a.mujica_characters=[1,1,2,0][i];});
assert.deepEqual(ids({order:'mygo_desc',include_used:true}),[3,1,4,2]);
assert.deepEqual(ids({order:'mujica_desc',include_used:true}),[2,1,3,4]);
for(const band of ['dream','millsage','dumb']){
  snapshot.accounts.forEach((a,i)=>a[band+'_characters']=[1,2,0,0][i]);
  assert.deepEqual(ids({order:band+'_desc',include_used:true}),[1,3,4,2]);
}
for(const order of ['price_desc','price_asc','mygo_desc','mujica_desc','selected_desc','desc','asc']){
  assert.deepEqual(ids({order,cards:'member:1,support:1',include_used:true}),[3]);
  assert.deepEqual(ids({order,cards:'member:1,support:1',allow_missing:'true',include_used:true}).sort((a,b)=>a-b),[1,2,3]);
  assert.deepEqual(ids({order,cards:'member:1,support:1',allow_missing:'false',include_used:true}),[3]);
  assert.deepEqual(ids({order,cards:'member:1,support:1',allow_missing:true,pull_count:'62'}),[2]);
  assert.deepEqual(ids({order,cards:'member:1,support:1',allow_missing:true,index:'4'}),[]);
  if(order!=='selected_desc')assert.deepEqual(ids({order,allow_missing:true,include_used:true}).sort((a,b)=>a-b),[1,2,3,4]);
}
delete snapshot.meta.public_account_limit;
snapshot.accounts.forEach(a=>{a.used=false;});
(async () => {
  let calls = [];
  let current = snapshot;
  let race = false;
  sandbox.fetch = async url => {
    calls.push(url);
    if (url === "data/current.json") return {ok:true, json:async()=>({revision:current.revision})};
    if (race) {race = false; return {ok:false};}
    return {ok:true, json:async()=>current};
  };
  assert.equal((await sandbox.source.query(options)).total, 4);
  assert.equal(calls.length, 2);
  await sandbox.source.query({...options,cards:"member:1"});
  assert.equal(calls.length, 2, "Filtering must not fetch data");
  await sandbox.source.query(options, undefined, true);
  assert.equal(calls.length, 3, "Unchanged snapshot only fetches its pointer");
  current = {...snapshot, revision:"b".repeat(64), cards:[{key:"support:1"}], accounts:[snapshot.accounts[2]]};
  race = true;
  const retired = await sandbox.source.query({...options,cards:"member:1",page:20}, undefined, true);
  assert.equal(retired.total, 0);
  assert.equal(retired.page, 1);
  assert.equal(calls.length, 7, "A deployment race retries the pointer and snapshot once");
  const retained = {cards:'support:1',order:'price_asc',page:2,page_size:1,include_used:true,allow_missing:true,pull_count:'62'};
  current = {...snapshot, revision:'c'.repeat(64), accounts:[snapshot.accounts[2],{...snapshot.accounts[2],index:5},{...snapshot.accounts[2],index:6,used:true}]};
  const beforeOptions = JSON.stringify(retained);
  const updated = await sandbox.source.query(retained, undefined, true);
  assert.equal(JSON.stringify(retained), beforeOptions, 'Automatic refresh keeps the selected cards, sort, page, size and used/OR/pull filters');
  assert.equal(updated.page,2);assert.equal(updated.pages,3);assert.deepEqual(Array.from(updated.accounts,a=>a.index),[5]);
  console.log("Client checks passed: AND/OR independent of sort/band counts/kind identity/count/page/expired card/refresh race");
})().catch(error => {console.error(error); process.exitCode = 1;});
