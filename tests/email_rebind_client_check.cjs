// Exercise the real UI with startup, cleanup and uncertain-result responses; no network or game.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ournotes/rebind/static/app.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(value, options={}) {
  const elements = new Map(), calls = [], timers = [], copied = [];
  const el = id => {
    if (!elements.has(id)) elements.set(id, {value:'', type:'password', hidden:false, disabled:false, textContent:'', dataset:{}, focus() {}, select() {this.selected=true;}, addEventListener() {}, checkValidity:() => true});
    return elements.get(id);
  };
  el('account-index').value = '1';
  const html = fs.readFileSync(path.join(__dirname, '../ournotes/rebind/static/view.html'), 'utf8');
  for (const name of ['rebind-message-sent','rebind-message-receipt']) el(name).textContent = html.match(new RegExp(`id="${name}"[^>]*>([^<]*)</p>`))[1];
  assert.ok(!html.includes('<textarea') && !html.includes('rebind-show'));
  assert.equal((html.match(/aria-label="复制/g) || []).length, 3);
  let stateListener;
  const context = vm.createContext({
    window:{consoleState:{subscribe(listener){stateListener=listener;listener({email_rebind:value});}}},
    document: {getElementById:el, querySelector:() => ({content:'test-only'}), createRange:() => ({selectNodeContents(field) {this.field=field;}}), getSelection:() => ({removeAllRanges() {},addRange(range) {range.field.selected=true;}})},
    navigator:{clipboard:{writeText:async text => {if(options.clipboardFails) throw new Error('synthetic failure');copied.push(text);}}},
    crypto: {randomUUID:() => 'b'.repeat(32)},
    setTimeout:fn => {timers.push(fn); return timers.length;}, clearTimeout() {},
    fetch:async (url, options) => {
      calls.push({url, options});
      if (url.endsWith('/password') && options?.method === 'POST' && value.passwordResponse) return value.passwordResponse;
      return {ok:true, json:async () => url.endsWith('/password') ? {password:'test-password'} : url.endsWith('/stop') ? {task:null,running:false} : url.endsWith('/eligibility') ? {index:1,used:false} : value};
    },
  });
  vm.runInContext(source, context);
  return {el, calls, timers, copied, setValue(next) {value=next;}, publish() {stateListener({email_rebind:value});}};
}
(async () => {
  const task = {id:'a'.repeat(32),index:1,instance:0,state:'error',message:'本机运行数据库正被占用，未启动换绑。',error:{
    phase:'preflight',code:'runtime_db_busy',source:'runtime-db',exception_type:'OperationalError',sqlite_errorcode:5,
    hint:'清除本次未启动任务后重试。',occurred_at:'2026-10-07T05:00:00Z',diagnostic_saved:true,
  }};
  const blocked = fixture({task,running:false}); await tick();
  assert.equal(blocked.el('rebind-start').textContent, '未启动换绑');
  assert.equal(blocked.el('rebind-start').disabled, true);
  assert.equal(blocked.el('rebind-confirm').disabled, true);
  assert.equal(blocked.el('rebind-stop').textContent, '清除任务');
  assert.equal(blocked.el('rebind-secret').hidden, false);
  assert.equal(blocked.el('rebind-error-details').hidden, false);
  assert.match(blocked.el('rebind-error-info').textContent, /runtime_db_busy.*SQLite 错误码：5/);
  assert.match(blocked.el('rebind-error-hint').textContent, /清除/);
  await blocked.el('rebind-stop').onclick();
  assert.equal(blocked.el('rebind-error-details').hidden, true);
  assert.equal(blocked.el('rebind-error-hint').hidden, true);
  assert.equal(blocked.el('rebind-start').textContent, '开始换绑');
  assert.equal(blocked.el('rebind-start').disabled, false);
  assert.equal(blocked.calls.filter(call => call.options.method === 'POST'&&!call.url.endsWith('/password')).length, 2);
  assert.ok(blocked.calls.at(-1).url.endsWith('/eligibility'));
  const cleaning = fixture({task,running:true}); await tick();
  assert.equal(cleaning.el('rebind-stop').textContent, '停止换绑');
  const uncertain = fixture({task:{...task,error:undefined,message:'换绑结果待核查'},running:false}); await tick();
  assert.equal(uncertain.el('rebind-start').textContent, '需要核查');
  assert.equal(uncertain.el('rebind-stop').textContent, '停止换绑');
  assert.equal(uncertain.el('rebind-error-details').hidden, true);
  assert.ok(uncertain.calls.every(call => call.options.method !== 'POST'||call.url.endsWith('/password')));
  for (const running of [false,true]) {
    const completed = fixture({task:{...task,state:'success',error:undefined,message:'换绑成功'},running},{clipboardFails:true}); await tick();
    assert.equal(completed.el('rebind-start').textContent, '换绑成功');
    assert.equal(completed.el('rebind-stop').textContent, '结束换绑');
    assert.equal(completed.el('rebind-stop').className, 'button secondary');
    assert.equal(completed.el('rebind-stop').disabled, false);
    assert.equal(completed.el('rebind-secret').hidden, false);
    assert.equal(completed.el('rebind-password').textContent,'好啦，当前密码是 test-password ~');
    await completed.el('rebind-copy').onclick();
    assert.equal(completed.el('rebind-password').selected,true);
    await completed.el('rebind-stop').onclick();
    assert.equal(completed.calls.filter(call=>call.options.method==='POST'&&!call.url.endsWith('/password')).length,2);
    assert.ok(completed.calls.at(-1).url.endsWith('/eligibility'));
    assert.ok(!completed.el('rebind-password').textContent.includes('test-password'));
    assert.equal(completed.el('rebind-copy').disabled,true);
  }
  const ending = fixture({task:{...task,state:'ending',error:undefined},running:true}); await tick();
  assert.equal(ending.el('rebind-stop').textContent,'结束中…');
  assert.equal(ending.el('rebind-stop').className,'button secondary');
  assert.equal(ending.el('rebind-stop').disabled,true);
  assert.equal(cleaning.el('rebind-stop').className,'button danger');
  const login = fixture({task:{...task,state:'login',error:undefined},running:true}); await tick();
  assert.equal(login.el('rebind-password').textContent,'好啦，当前密码是 test-password ~');
  const callsBeforeCopy=login.calls.length;
  await login.el('rebind-copy-sent').onclick();
  await login.el('rebind-copy').onclick();
  await login.el('rebind-copy-receipt').onclick();
  assert.deepEqual(login.copied,[login.el('rebind-message-sent').textContent,'好啦，当前密码是 test-password ~',login.el('rebind-message-receipt').textContent]);
  assert.equal(login.calls.length,callsBeforeCopy);
  let resolvePassword;
  const response = new Promise(resolve => {resolvePassword=resolve;});
  const stale = fixture({task:{...task,state:'login',error:undefined},passwordResponse:response}); await tick();
  stale.setValue({task:{...task,state:'ending',error:undefined},running:true});
  stale.publish();
  resolvePassword({ok:true,json:async()=>({password:'stale-secret'})}); await tick();
  assert.ok(!stale.el('rebind-password').textContent.includes('stale-secret'));
  assert.equal(stale.el('rebind-copy').disabled,true);
  const interrupted = fixture({task:{...task,state:'interrupted',error:undefined},running:false}); await tick();
  assert.match(interrupted.el('rebind-password').textContent,/密码已丢失/);
  assert.ok(interrupted.calls.every(call=>!call.url.endsWith('/password')));
  const waitingTask={...task,error:undefined,email:'registered@gmail.com',state:'waiting_email',email_revision:1,message:'邮箱已注册，请更换邮箱'};
  const replacement = fixture({task:waitingTask,running:true}); await tick();
  assert.equal(replacement.el('rebind-email').disabled,false);
  assert.equal(replacement.el('rebind-instance').disabled,true);
  assert.equal(replacement.el('rebind-target').hidden,false);
  assert.equal(replacement.el('rebind-confirm').disabled,true);
  replacement.el('rebind-email').value='new@qq.com';
  replacement.publish();
  assert.equal(replacement.el('rebind-email').value,'new@qq.com');
  await replacement.el('rebind-target').onclick();
  const sent = replacement.calls.filter(call=>call.url.endsWith('/target'));
  assert.equal(sent.length,1);
  assert.deepEqual(JSON.parse(sent[0].options.body),{task_id:task.id,email_revision:1,email:'new@qq.com',command_id:'b'.repeat(32)});
  assert.equal(replacement.el('rebind-password').textContent,'好啦，当前密码是 test-password ~');
  replacement.setValue({task:{...waitingTask,email:'new@qq.com',state:'changing_email'},running:true});
  replacement.publish();
  assert.equal(replacement.el('rebind-email').disabled,true);
  assert.equal(replacement.el('rebind-target').hidden,true);
  process.stdout.write('email rebind client checks passed\n');
})().catch(error => {process.stderr.write(String(error.stack));process.exitCode=1;});
