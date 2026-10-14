// Synthetic DOM checks for pagination, preset identity and request contracts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const tick = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.listeners = {};
    this.textContent = ''; this.disabled = false; this.hidden = false;
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  get value() {return this._value ?? (this.tagName === 'SELECT' ? this.options[0]?.value ?? '' : '');}
  set value(value) {this._value = String(value);}
  get options() {return this.children;}
  append(...values) {
    for (const value of values) {
      if (value.tagName === 'FRAGMENT') this.append(...value.children);
      else {value.parent = this; this.children.push(value);}
    }
  }
  replaceChildren(...values) {this.children = []; if (this.tagName === 'SELECT') delete this._value; this.append(...values);}
  remove() {this.parent.children = this.parent.children.filter(value => value !== this);}
  setAttribute(key, value) {this[key] = String(value);}
  removeAttribute(key) {delete this[key];}
  setCustomValidity(value) {this.validation = value;}
  focus() {}
  scrollIntoView() {}
  addEventListener(type, handler) {(this.listeners[type] ||= []).push(handler);}
  emit(type) {return Promise.all((this.listeners[type] || []).map(handler => handler({currentTarget:this, preventDefault() {}})));}
  querySelector(selector) {
    const field = /^\[data-field="([^"]+)"\]$/.exec(selector);
    const matches = value => field ? value.dataset.field === field[1] :
      value.tagName === 'BUTTON' && (selector === 'button' || value.type === 'submit');
    for (const child of this.children) {if (matches(child)) return child; const found = child.querySelector(selector); if (found) return found;}
    return null;
  }
}

function fixture() {
  const html = fs.readFileSync(path.join(__dirname, '../web/admin/index.html'), 'utf8');
  const nodes = Object.fromEntries([...html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"/g)].map(match => [match[2], new Element(match[1])]));
  nodes['accounts-form'].append(nodes['accounts-submit']); nodes['accounts-submit'].type = 'submit';
  const save = new Element('button'); save.type = 'submit';
  nodes['preset-form'].append(nodes['preset-fields']); nodes['preset-fields'].append(save);
  const requests = [];
  const context = vm.createContext({URLSearchParams, Date, Number, Object, Promise,
    URL: {createObjectURL:() => 'blob:synthetic-preview', revokeObjectURL() {}},
    Option: function(text, value) {const option = new Element('option'); option.textContent = text; option.value = value; return option;},
    document: {getElementById:id => nodes[id], createElement:tag => new Element(tag), createDocumentFragment:() => new Element('fragment'), querySelectorAll:() => []},
    window: {addEventListener() {}, confirm:() => true}, location: {hash:'',protocol:'http:',host:'fixture.test'},
    fetch: (url, options) => {
      if (url === '/admin/api/session') return Promise.resolve({ok:false,status:401,json:async () => ({detail:'合成会话尚未登录'})});
      return new Promise(resolve => requests.push({url,options,reply(value, status = 200) {resolve({ok:status < 400,status,json:async () => value,blob:async () => value});}}));
    }, setTimeout() {}, crypto: {randomUUID:() => 'synthetic-request'}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/admin/app.js'), 'utf8'), context);
  return {nodes,requests,context,run:source => vm.runInContext(source, context)};
}
const snapshot = {version:4,pools:[
  {key:'pool-12',name:'合成梦限卡池',gacha_id:12,supported:true,resources:['stars','ticket']},
  {key:'pool-4',name:'合成常驻卡池',gacha_id:4,supported:true,resources:['stars']}],
  presets:{'0':{name:'预设零',description:'',entries:[{pool_key:'pool-12',resource:'ticket',pulls:43}]}}};
const options = [{id:'0',name:'预设零',deleted:false,count:40},{id:'__unknown__',name:'历史未记录/未匹配',deleted:false,count:5}];
const page = offset => ({total:45,offset,limit:20,preset_options:options,accounts:[
  {result:{index:8 + offset,used:false},owner_username:'合成用户',projection:{ssr_total:1}}]});

(async () => {
  const {nodes,requests,context,run} = fixture();
  await tick(); run("loggedIn({username:'Fuyuki',is_superadmin:true})");
  const first = run('accounts(0)');
  assert.match(requests[0].url, /limit=20/);
  assert.equal(nodes.previous.disabled, true); assert.equal(nodes.next.disabled, true);
  assert.match(nodes['accounts-loading'].textContent, /正在读取/);
  await nodes.next.emit('click'); await nodes.previous.emit('click');
  assert.equal(requests.length, 1, 'Repeated clicks must not send another request while loading');
  requests[0].reply(page(0)); await first;
  assert.equal(nodes.page.textContent, '1 / 3'); assert.equal(nodes.next.disabled, false);
  nodes.next.emit('click'); nodes.next.emit('click');
  assert.equal(requests.length, 2); assert.match(requests[1].url, /offset=20/);
  requests[1].reply({}, 503); await tick(); await tick();
  assert.equal(nodes.page.textContent, '1 / 3', 'A failed request must leave the current page intact');
  nodes.next.emit('click'); assert.match(requests[2].url, /offset=20/);
  requests[2].reply(page(20)); await tick(); await tick();
  assert.equal(nodes.page.textContent, '2 / 3');
  nodes['account-preset'].value = '0';
  const filtered = run('accounts(0)'); assert.match(requests[3].url, /preset_id=0/);
  requests[3].reply({...page(0),total:19}); await filtered;
  assert.equal(nodes['account-preset'].value, '0'); assert.equal(nodes.next.disabled, true);
  nodes['account-preset'].value = '__unknown__';
  const unknown = run('accounts(0)'); assert.match(requests[4].url, /preset_id=__unknown__/);
  requests[4].reply({...page(0),total:5}); await unknown;

  context.snapshot = snapshot; run('showPresets(snapshot)');
  assert.equal(nodes['preset-select'].value, '0');
  assert.match(nodes['preset-select'].options[0].textContent, /^#0/);
  let row = nodes['preset-entries'].children[0];
  const pool = row.querySelector('[data-field="pool_key"]');
  const resource = row.querySelector('[data-field="resource"]');
  assert.equal(pool.tagName, 'SELECT'); assert.match(pool.options[0].textContent, /合成梦限卡池/);
  assert.equal(resource.value, 'ticket');
  pool.value = 'pool-4'; await pool.emit('change');
  assert.equal(resource.options.length, 1); assert.equal(resource.value, 'stars');
  const update = nodes['preset-form'].emit('submit');
  assert.equal(requests[5].url, '/admin/api/presets/0'); assert.equal(requests[5].options.method, 'PUT');
  assert.deepEqual(JSON.parse(requests[5].options.body).preset.entries, [{pool_key:'pool-4',resource:'stars',pulls:43}]);
  await nodes['preset-form'].emit('submit'); assert.equal(requests.length, 6);
  requests[5].reply({...snapshot,version:5}); await update;
  await nodes['preset-new'].emit('click'); assert.equal(nodes['preset-select'].value, '');
  assert.equal(nodes['preset-delete'].disabled, true);
  nodes['preset-name'].value = '新增预设'; row = nodes['preset-entries'].children[0];
  const create = nodes['preset-form'].emit('submit');
  assert.equal(requests[6].url, '/admin/api/presets'); assert.equal(requests[6].options.method, 'POST');
  const body = JSON.parse(requests[6].options.body);
  assert.equal(Object.hasOwn(body, 'key'), false);
  requests[6].reply({...snapshot,version:6,created_id:1,presets:{...snapshot.presets,'1':body.preset}}); await create;
  assert.equal(nodes['preset-select'].value, '1'); assert.match(nodes['preset-status'].textContent, /#1 已保存/);
  const remove = nodes['preset-delete'].emit('click');
  assert.equal(requests[7].options.method, 'DELETE'); assert.match(requests[7].url, /presets\/1\?expected_version=6/);
  requests[7].reply({...snapshot,version:7}); await remove;
  assert.equal(nodes['preset-select'].value, '0');

  const historyRead = run('detail(8)');
  assert.equal(requests[8].url, '/admin/api/accounts?index=8');
  requests[8].reply({accounts:[{owner_username:'合成用户',usage_status:'used',version:1,
    projection:{ssr_total:1},ssr_cards:[],historical_preset:{id:null,name:'历史未记录/未匹配',deleted:false},
    result:{index:8,email:'synthetic@example.test',pull_count:43,draw_labels:[],used:true,
      preset_snapshot:{key:'0',version:1,plan:[{pool_key:'pool-12',resource:'stars',pulls:43}]}}}]});
  await historyRead;
  const history = nodes['account-detail'].children.find(value => value.tagName === 'DETAILS');
  const text = history.children.map(value => value.textContent).join(' ');
  assert.doesNotMatch(text, /预设 #0|pool-12/, 'A server-resolved unknown historical identity must not be inferred from a numeric name');
  assert.match(text, /合成梦限卡池/);
  const generate = nodes['account-detail'].children[0].children.find(value => value.textContent === '生成预览图');
  const preview = nodes['account-detail'].children.find(value => value.className === 'account-preview');
  const failedPreview = generate.emit('click');
  assert.equal(generate.disabled, true); assert.equal(generate.textContent, '生成中…');
  assert.equal(requests.at(-1).url, '/admin/api/accounts/8/preview.png');
  requests.at(-1).reply({detail:'预览图暂时不可用'}, 503); await failedPreview;
  assert.equal(preview.hidden, true); assert.equal(generate.disabled, false);
  assert.equal(nodes.notice.textContent, '预览图暂时不可用');
  const generated = generate.emit('click'); requests.at(-1).reply('png-data'); await generated;
  assert.equal(preview.hidden, false);
  assert.equal(preview.children[1].download, '账号-8-预览图.png');
  assert.equal(preview.children[2].src, 'blob:synthetic-preview');
  const late = generate.emit('click'); run('detailRequest++');
  preview.replaceChildren(); requests.at(-1).reply('late-png'); await late;
  assert.equal(preview.children.length, 0, 'A late response must not replace another account preview');
  nodes['scope'].value = 'unused'; nodes['statistics-preset'].value = '0';
  const stats = run('statistics()');
  const req = requests.at(-1);
  assert.match(req.url, /scope=unused/); assert.match(req.url, /preset_id=0/);
  req.reply({n:1,observed_at:1000,preset_options:options,metrics:[{key:'current_up_count',label:'当期 UP 数量',max:1,mean:1,indices:[8],histogram:[{value:1,count:1,percent:100}]}]});
  await stats;
  const record = nodes.tables.children[0].children.find(x => x.tagName === 'DETAILS');
  assert.equal(record.children[0].textContent, '最高记录编号');
  assert.equal(nodes['statistics-preset'].value, '0');
  console.log('Admin client checks passed: pagination guards, failure recovery, preset filters, numeric identity, pool resources and CRUD.');
})().catch(error => {console.error(error); process.exitCode = 1;});
