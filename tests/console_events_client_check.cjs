// State sharing, WS authentication, and coordinated HTTP fallback use synthetic data only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const tick = () => new Promise(resolve => setImmediate(resolve));
const requests = [], sockets = [], timers = new Map(), revisions = [], visibility = {};
let timerId = 0;
class Socket {
  static CONNECTING = 0; static OPEN = 1;
  constructor(url, protocols) {this.url=url;this.protocols=protocols;this.readyState=0;this.listeners={};sockets.push(this);}
  addEventListener(name, handler) {this.listeners[name]=handler;}
  emit(name, event={}) {this.listeners[name]?.(event);}
  close() {this.readyState=3;this.emit('close');}
}
const context = vm.createContext({
  window: {addEventListener() {}},
  document: {hidden:false, querySelector:()=>({content:'synthetic-token'}), addEventListener:(name,fn)=>visibility[name]=fn},
  location: {protocol:'http:',host:'127.0.0.1:18866'}, WebSocket:Socket, console,
  setTimeout:(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;}, clearTimeout:id=>timers.delete(id),
  fetch:async (url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({stream_revision:requests.length,running:false})};}
});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/launcher/events.js'),'utf8'),context);
(async()=>{
  const state=context.window.consoleState;
  state.subscribe(value=>revisions.push(value.stream_revision));
  state.subscribe(()=>{});
  state.start();state.start();
  await tick();
  assert.equal(sockets.length,1);assert.equal(requests.length,1);
  assert.equal(sockets[0].url,'ws://127.0.0.1:18866/api/launcher/events');
  assert.deepEqual([...sockets[0].protocols],['ournotes','session.synthetic-token']);
  sockets[0].readyState=1;sockets[0].emit('open');
  sockets[0].emit('message',{data:JSON.stringify({type:'snapshot',data:{stream_revision:5,running:false}})});
  sockets[0].emit('message',{data:JSON.stringify({type:'snapshot',data:{stream_revision:4,running:false}})});
  assert.equal(state.snapshot.stream_revision,5);
  assert.equal(timers.size,0);
  await Promise.all([state.refresh(true),state.refresh(true)]);
  assert.equal(requests.length,2);assert.equal(requests[1].options.method,'POST');
  assert.equal(requests[1].options.headers['X-Launcher-Token'],'synthetic-token');
  sockets[0].close();assert.equal(timers.size,1);
  const timer=[...timers.values()][0];timers.clear();await timer.fn();
  assert.equal(requests.length,3);assert.equal(sockets.length,2);
  state.stop();assert.equal(timers.size,0);
  assert.equal(revisions.includes(4),false);
  process.stdout.write('console events client checks passed\n');
})().catch(error=>{console.error(error);process.exitCode=1;});
