// Tests for the three direct account actions. All account data and requests are synthetic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const tick = () => new Promise(resolve => setImmediate(resolve));
const source = fs.readFileSync(path.join(__dirname, '../web/launcher/account-actions.js'), 'utf8');
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
class Element {
  constructor() {
    this.listeners = {}; this.textContent = ''; this.disabled = false; this.open = false;
    this.classList = {toggle() {}};
  }
  addEventListener(type, fn) {(this.listeners[type] ||= []).push(fn);}
  setAttribute() {}
  emit(type) {
    const event = {preventDefault() {this.defaultPrevented = true;}};
    for (const fn of this.listeners[type] || []) fn(event);
    return event;
  }
  showModal() {this.open = true;}
  close() {this.open = false; this.emit('close');}
}
function fixture(extra = {}) {
  const names = ['input','usedButton','bookedButton','dialog','title','summary','warning','progress','confirm','cancel'];
  const elements = Object.fromEntries(names.map(name => [name, new Element()]));
  const context = vm.createContext({setTimeout:fn=>fn, clearTimeout() {}});
  vm.runInContext(source+'\nglobalThis.factory=createAccountActions;', context);
  let index = 1, state = 'available', version = 1;
  const writes = [], messages = [];
  const options = {...elements, getIndex:()=>index, isBusy:()=>false,
    preview:async number=>({index:number,usage_status:state,usage_protocol:2,version,token:'synthetic'}),
    write:async (value,target)=>{writes.push(target);state=target;version++;return {message:'已确认'};},
    onMessage:message=>messages.push(message), onChange:async()=>{}, ...extra};
  const actions = context.factory(options);
  return {...elements, actions, writes, messages, options, setIndex(number) {index=number;elements.input.emit('input');}};
}
(async () => {
  const booking = fixture();
  await booking.actions.refresh();
  booking.bookedButton.emit('click'); await tick();
  assert.equal(booking.title.textContent,'标记已拍下');
  booking.confirm.emit('click'); await tick();
  assert.deepEqual(booking.writes,['booked']);
  assert.equal(booking.bookedButton.textContent,'标记未拍下');
  booking.bookedButton.emit('click'); await tick();
  assert.equal(booking.title.textContent,'标记未拍下');
  assert.match(booking.warning.textContent,/核查保护继续保留/);
  booking.confirm.emit('click'); await tick();
  assert.deepEqual(booking.writes,['booked','available']);
  assert.equal(booking.bookedButton.textContent,'标记已拍下');

  const terminal = fixture();
  await terminal.actions.refresh();
  terminal.usedButton.emit('click'); await tick();
  assert.equal(terminal.title.textContent,'标记已使用');
  assert.match(terminal.warning.textContent,/不可撤销/);
  terminal.confirm.emit('click'); await tick();
  assert.deepEqual(terminal.writes,['used']);
  assert.equal(terminal.usedButton.disabled,true);
  assert.equal(terminal.bookedButton.disabled,true);

  const write = deferred(); let submissions = 0;
  const pending = fixture({write:()=>{submissions++;return write.promise;}});
  pending.usedButton.emit('click'); await tick();
  pending.confirm.emit('click'); pending.confirm.emit('click');
  assert.equal(submissions,1); assert.equal(pending.cancel.disabled,true);
  assert.equal(pending.dialog.emit('cancel').defaultPrevented,true);
  write.resolve({message:'已确认'}); await tick();

  const old = deferred(), fresh = deferred();
  const race = fixture({preview:number=>number===1?old.promise:fresh.promise});
  const first=race.actions.refresh(); race.setIndex(2); const second=race.actions.refresh();
  fresh.resolve({index:2,usage_status:'available',usage_protocol:2,version:1}); await second;
  old.resolve({index:1,usage_status:'booked',usage_protocol:2,version:10}); await first;
  assert.equal(race.bookedButton.textContent,'标记已拍下');
  race.bookedButton.emit('click'); await tick();
  assert.match(race.summary.textContent,/账号 2/);

  let reads=0;
  const mismatch = fixture({preview:async number=>({index:number,usage_status:'booked',usage_protocol:2,version:++reads,used:true}),
    write:async()=>{throw new Error('响应丢失');}});
  mismatch.usedButton.emit('click'); await tick(); mismatch.confirm.emit('click'); await tick();
  assert.equal(mismatch.dialog.open,true); assert.equal(mismatch.messages.length,0);
  assert.match(mismatch.summary.textContent,/已拍下/);

  let calls=0, attempts=0;
  const unknown = fixture({preview:async number=>{
    if (++calls===2) throw new Error('离线');
    return {index:number,usage_status:'available',usage_protocol:2,version:1};
  },write:async()=>{attempts++;throw new Error('超时');}});
  unknown.usedButton.emit('click'); await tick(); unknown.confirm.emit('click'); await tick();
  assert.equal(unknown.confirm.textContent,'重新查询');
  unknown.confirm.emit('click'); await tick();
  assert.equal(attempts,1); assert.equal(calls,3);
  console.log('Account action button checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
