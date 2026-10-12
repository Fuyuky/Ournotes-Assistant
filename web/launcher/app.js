"use strict";
function createLaneRecovery({api, el, onChange, onMessage, storage = sessionStorage}) {
  const pending = new Map();
  let revision = 0;
  try {for (const value of JSON.parse(storage.getItem('lane-recovery') || '[]')) pending.set(value.key, value);} catch {}
  function changed() {
    revision++;
    try {storage.setItem('lane-recovery', JSON.stringify([...pending.values()].map(({checking, ...v}) => v)));} catch {}
    onChange();
  }
  const keyFor = (batch, lane) => `${batch.id}:${lane.run_id}`;
  async function check(value) {
    if (value.checking || pending.get(value.key) !== value) return;
    value.checking = true;
    try {
      const receipt = await api(`operations/${value.command_id}`);
      if (pending.get(value.key) !== value) return;
      if (['succeeded', 'failed', 'paused', 'needs_review'].includes(receipt.status)) {
        pending.delete(value.key);
        onMessage(receipt.result?.message || (receipt.status === 'succeeded' ? '已交给正常流程，请查看实例进展。' : value.error || '恢复未完成，请查看实例暂停原因。'));
      } else value.message = receipt.status === 'accepted' ? '等待调度器接收…' : '正在恢复，等待工作线程确认…';
    } catch {value.message = '回执暂未确认，正在查询原请求；请勿重复操作。';}
    finally {value.checking = false; changed();}
  }
  async function start(batch, lane, mode) {
    const key = keyFor(batch, lane);
    if (pending.has(key) || !lane.recovery?.[mode]?.enabled) return;
    const value = {key, batch_id: batch.id, instance: lane.instance, command_id: crypto.randomUUID(), mode, message: '正在提交恢复请求…'};
    pending.set(key, value); changed();
    try {
      const result = await api('action', {action: 'resume_lane', command_id: value.command_id,
        batch_id: batch.id, instance: lane.instance, recovery_mode: mode, expected_version: lane.recovery.version});
      value.message = result.message || '已提交，等待调度器确认…';
    } catch (error) {
      // A lost response is not permission to send another mutation.
      value.message = '响应未确认，正在查询原请求回执…';
      value.error = error.message;
      if (pending.get(key) === value) onMessage(error.message);
    }
    changed(); await check(value);
  }
  function render(batch, lane) {
    const value = pending.get(keyFor(batch, lane));
    if (lane.status !== 'paused') return null;
    const box = el('div', 'lane-recovery');
    const buttons = el('div', 'lane-recovery-buttons');
    for (const [mode, label] of [['retry', '重试当前步骤'], ['restart_game', '重启游戏并继续']]) {
      const capability = lane.recovery?.[mode];
      const button = el('button', 'button secondary', value?.mode === mode ? value.message : label);
      button.type = 'button'; button.disabled = !!value || !capability?.enabled;
      button.title = capability?.reason || (mode === 'retry' ? '从保存的当前阶段重新观察并继续。' : '重启本实例的游戏，再核对原账号继续。');
      button.setAttribute('aria-label', `MuMu ${lane.instance} ${label}`);
      button.setAttribute('aria-busy', String(!!value && value.mode === mode));
      button.classList.toggle('is-loading', !!value && value.mode === mode);
      button.addEventListener('click', () => start(batch, lane, mode));
      buttons.append(button);
    }
    box.append(buttons);
    const reasons = lane.recovery ? [...new Set(['retry','restart_game'].map(m => lane.recovery[m]?.reason).filter(Boolean))] : ['当前控制台尚未加载恢复接口；请先安全暂停，退出并重新启动控制台。'];
    if (value) box.append(el('small', 'lane-recovery-note', value.message));
    else for (const reason of reasons) box.append(el('small', 'lane-recovery-note', reason));
    return box;
  }
  return {render, start, get revision() {return revision;},
    poll(batch, lanes = []) {
      if (!batch) return;
      let cleared = false;
      for (const value of pending.values()) {
        if (value.batch_id !== batch.id) continue;
        const lane = lanes.find(lane => keyFor(batch, lane) === value.key);
        // Live progress supersedes an old recovery receipt, including after a reload.
        if (lane && ['running', 'complete', 'cancelled'].includes(lane.status)) {
          pending.delete(value.key); cleared = true;
        } else check(value);
      }
      if (cleared) changed();
    }};
}

"use strict";
function batchStartControl(data, pending) {
  const normal = {disabled:false, label:'开启新批次', hint:''};
  if (pending) return {disabled:true, label:'正在处理…', hint:'正在提交控制操作，请稍候。'};
  if (data.error) return {disabled:true, label:normal.label, hint:data.error};
  if (data.preset_error || !data.presets?.length) return {disabled:true,label:normal.label,hint:data.preset_error || '共享预设尚未就绪，请配置云端连接。'};
  if (data.running) return {disabled:true, label:normal.label, hint:'运行锁正被占用。请等待调度或邮箱换绑任务结束后再开启新批次。'};
  if (data.batch && !['complete','cancelled'].includes(data.batch.status)) return {disabled:true, label:normal.label, hint:'已有未结束批次。暂停并等进程退出后，点击“结束当前批次”，即可开始下一批。'};
  const busy = data.jobs?.find(j=>j.status==='running' && j.resources.some(r=>['device','mail'].includes(r)));
  if (busy) return {disabled:true, label:normal.label, hint:`${busy.title || '设备或邮件任务'}正在执行，请等待任务结束。`};
  return normal;
}
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="launcher-token"]').content;
const state = {status:null, initialized:false, polling:false, pending:false, selectedJob:null, renderedJobs:"", quitting:false};
const names = {overview:"总览",runner:"运行控制",presets:"抽卡预设",accounts:"账号管理",connections:"接入与检查"};
const statuses = {running:"运行中",paused:"已暂停",complete:"已完成",cancelled:"已结束",waiting_start:"等待启动",starting:"正在启动",success:"已完成",failed:"未成功"};
const phases = {switch_account:"切换账号",login:"注册 / 登录",rewards:"开屏奖励",gifts:"邮箱奖励",draws:"招募与识别",logout:"保存并退出",finished:"已完成"};
function el(tag, cls, text) {const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
function duration(seconds) {if(!seconds)return "—";const n=Math.round(seconds);return n>=3600?`${Math.floor(n/3600)} 小时 ${Math.floor(n%3600/60)} 分`:`${Math.floor(n/60)} 分 ${n%60} 秒`;}
function toast(message) {$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,6500);}
async function api(path,body) {const response=await fetch(`/api/launcher/${path}`,{cache:"no-store",headers:body===undefined?{}:{"Content-Type":"application/json","X-Launcher-Token":token},...(body===undefined?{}:{method:"POST",body:JSON.stringify(body)})});let data;try{data=await response.json();}catch{throw new Error("无法读取服务响应，请确认启动器仍在运行。");}if(!response.ok)throw new Error(data.detail||"操作未完成。");return data;}
function navigate() {const view=location.hash.slice(1)||"overview";const active=names[view]?view:"overview";document.querySelectorAll('.view').forEach(v=>v.hidden=v.id!==`view-${active}`);document.querySelectorAll('[data-view]').forEach(n=>n.classList.toggle('active',n.dataset.view===active));$('page-name').textContent=names[active];}
window.addEventListener('hashchange',navigate);navigate();
function renderLanes(data) {
  const signature=JSON.stringify([data.batch,data.lanes,data.running,data.storage,laneRecovery.revision]);if(renderLanes.signature===signature)return;renderLanes.signature=signature;
  const batch=data.batch;document.querySelectorAll('.batch-state').forEach(n=>{const key=batch?.status||"none";n.textContent=data.running?"调度运行中":statuses[key]||"无批次";n.className=`badge batch-state ${data.running?'running':key}`;});
  document.querySelectorAll('.batch-description').forEach(n=>n.textContent=batch?`批次 ${batch.id.slice(0,8)} · ${data.lanes.length} 个实例 · ${data.running?'调度进程活动中':'当前没有活动调度进程'}`:"暂无批次。");
  document.querySelectorAll('.batch-time').forEach(n=>n.textContent=batch?`已记录活动时间 ${duration(batch.active_seconds)}`:"等待第一轮运行");
  $('debug-details').hidden=!(data.batch?.debug_mode||$('debug-mode').checked);
  $('process-state').textContent=data.running?"进程活动中":"进程未运行";
  document.querySelectorAll('.lane-list').forEach(list=>{const fragment=document.createDocumentFragment();for(const lane of data.lanes){const row=el('div','lane-row');const identity=el('div','lane-identity');const label=el('div');label.append(el('b','',`MuMu ${lane.instance}`),el('small','',`${statuses[lane.status]||lane.status}${lane.index!==null?' · #'+lane.index:''}`));identity.append(el('span','lane-icon','▯'),label);const middle=el('div','lane-center');middle.append(el('b','',lane.preset));const context=lane.message||[lane.index===null?null:`index ${lane.index}`,phases[lane.phase]||lane.phase, lane.transactions?`已识别 ${lane.transactions} 笔`:null].filter(Boolean).join(' · ')||"等待运行";const description=el('p','',context);description.title=context;middle.append(description);const recovery=laneRecovery.render(batch,lane);if(recovery)middle.append(recovery);const right=el('div','lane-right');const number=el('strong','',lane.processed);number.append(el('small','',data.batch?.count_mode==='shared'?' · 已处理':` / ${lane.target??'∞'}`));const progress=el('progress');progress.max=(data.batch?.count_mode==='shared'?data.batch.target:lane.target)||Math.max(lane.complete,1);progress.value=lane.processed;progress.setAttribute('aria-label',`MuMu ${lane.instance} 已处理 ${lane.processed}，云端已确认 ${lane.complete}，${data.batch?.count_mode==='shared'?'批次共享目标':'目标'} ${data.batch?.count_mode==='shared'?(data.batch.target??'持续运行'):(lane.target??'持续运行')}`);right.append(number,progress,el('small','',`云端已确认 ${lane.complete}`));row.append(identity,middle,right);fragment.append(row);}if(!data.lanes.length)fragment.append(el('div','empty-lanes','暂无运行实例'));list.replaceChildren(fragment);});
}
function renderJobs(jobs) {const signature=JSON.stringify(jobs.map(j=>[j.id,j.status]));if(signature!==state.renderedJobs){state.renderedJobs=signature;const list=document.querySelector('.job-list');const frag=document.createDocumentFragment();for(const job of jobs.slice(0,6)){const button=el('button','job-item');const mark=el('span',`job-indicator ${job.status}`,job.status==='success'?'✓':job.status==='failed'?'!':'◷');const label=el('span');label.append(el('b','',job.title),el('small','',new Date(job.started_at*1000).toLocaleTimeString('zh-CN',{hour12:false})));button.append(mark,label,el('em','',statuses[job.status]||job.status));button.addEventListener('click',()=>openJob(job.id));frag.append(button);}if(!jobs.length){const empty=el('div','job-empty');empty.append(el('span','','✧'),document.createTextNode('暂无任务。'));frag.append(empty);}list.replaceChildren(frag);}
}
function renderEnvironment(data){const e=data.environment;const items=[['Python',e.python,e.platform,true],['MuMu SDK',e.mumu?'已找到':'未找到','',e.mumu]];const signature=JSON.stringify(items);if(renderEnvironment.signature===signature)return;renderEnvironment.signature=signature;$('environment').replaceChildren(...items.map(([label,value,note,good])=>{const card=el('div',`environment-card ${good?'good':'warn'}`);card.append(el('b','',label),el('strong','',value));if(note)card.append(el('small','',note));return card;}));}
function resourceBusy(resource){return state.status?.jobs.some(j=>j.status==='running'&&j.resources.includes(resource));}
function controls() {const data=state.status;if(!data)return;const active=data.batch&&!['complete','cancelled'].includes(data.batch.status);const startControl=batchStartControl(data,state.pending);$('start').disabled=startControl.disabled;$('start').textContent=startControl.label;$('start-hint').textContent=startControl.hint;$('start-hint').hidden=!startControl.hint;document.querySelectorAll('[data-action]').forEach(button=>{const a=button.dataset.action;let disabled=state.pending;if(['resume','cancel','gmail_check','setup'].includes(a))disabled ||= data.running||!!data.error;if(['resume','cancel'].includes(a))disabled ||= !active||resourceBusy('device')||resourceBusy('mail');if(['pause','finish'].includes(a))disabled ||= !data.running||!active;if(['gmail_check','setup'].includes(a))disabled ||= resourceBusy('mail');button.disabled=disabled;});$('make-preview').disabled=state.pending;$('find-email').disabled=state.pending;accountActions.render();$('mumu-check').disabled=state.pending||resourceBusy('diagnostic');}
function syncPresetSelectors(){
  const presets=state.status.presets,signature=JSON.stringify(presets);
  if(syncPresetSelectors.signature===signature)return;syncPresetSelectors.signature=signature;
  document.querySelectorAll('#lane-inputs select').forEach(select=>{
    const value=select.value;select.replaceChildren(...presets.map(p=>{const option=el('option','',`${p.name} · 实际 ${p.total_pulls} 抽`);option.value=p.name;return option;}));select.value=presets.some(p=>p.name===value)?value:presets[0]?.name || '';
  });
}
function initialize(data){if(state.initialized)return;state.initialized=true;const lanes=data.lanes.length?data.lanes:[{instance:0,preset:'MyGO only'},{instance:1,preset:'Mujica only'},{instance:2,preset:'都抽'}];lanes.forEach(addLane);$('async-start').checked=!!data.batch?.async_start;runSummary();}
function serverUrl(fragment = '') {
  const service = state.status?.settings?.service_url;
  if (!service) throw new Error('请先在接入与检查中保存云端连接。');
  return service.replace(/\/$/, '') + '/admin/' + (fragment ? '#' + fragment : '');
}
function renderServerLinks() {
  const address = state.status?.settings?.service_url;
  $('server-admin').href = address ? address.replace(/\/$/, '') + '/admin/' : '#connections';
  $('server-admin').target = address ? '_blank' : '_self';
  document.querySelectorAll('[data-server-page]').forEach(link => {
    link.href = address ? serverUrl(link.dataset.serverPage) : '#connections';
    link.target = address ? '_blank' : '_self';
    link.rel = 'noopener';
  });
}
function receiveState(data) {
  if (state.quitting) return;
  const previousEvent = state.status?.cloud_sync?.event_revision;
  state.status=data;syncPresetSelectors();initialize(data);
  $('connection').className='connection connected';
  $('connection').replaceChildren(el('i'),document.createTextNode('本机已连接'));
  const processed=data.lanes.reduce((n,l)=>n+l.processed,0), confirmed=data.lanes.reduce((n,l)=>n+l.complete,0);
  $('valid-accounts').textContent=processed;
  $('excluded-accounts').textContent=data.lanes.length;
  $('art-count').textContent=data.cloud_sync?.pending ?? '—';
  $('batch-complete').textContent=data.batch?confirmed:'—';
  $('batch-goal').textContent=data.batch?`已处理 ${processed} / ${data.batch.target??'∞'} · 服务器确认完整成果`:'尚无运行批次';
  $('last-read').textContent=`更新于 ${new Date(data.observed_at*1000).toLocaleTimeString('zh-CN',{hour12:false})}`;
  $('notice').hidden=!data.error&&!data.preset_error;
  $('notice').textContent=data.error||data.preset_error||'';
  renderLanes(data);laneRecovery.poll(data.batch,data.lanes);renderJobs(data.jobs);renderEnvironment(data);
  if(data.updates)renderUpdates(data.updates);renderServerLinks();controls();
  if($('job-dialog').open&&state.selectedJob)updateJob().catch(()=>{});
  if (previousEvent !== undefined && data.cloud_sync?.event_revision !== previousEvent
      && ['ready','results.changed'].includes(data.cloud_sync?.event_type)
      && location.hash === '#accounts' && !$('account-query-result').hidden) {
    accountQuery.query(); accountActions.refresh();
  }
}
async function refresh() {
  if (state.quitting) return;
  try {return await window.consoleState.refresh(true);} catch {
    $('connection').className='connection offline';
    $('connection').replaceChildren(el('i'),document.createTextNode('连接中断'));
    $('notice').hidden=false;$('notice').textContent='无法连接本机控制台，页面保留上次读取的状态。';
  }
}
async function action(kind,extra={}) {
  if(state.pending)return;state.pending=true;controls();
  try {
    const result=await api('action',{action:kind,command_id:crypto.randomUUID(),...(['pause','resume','finish','cancel'].includes(kind)?{batch_id:state.status?.batch?.id}:{}),...extra});
    if(result.id){toast(`${result.title}已启动`);await refresh();await openJob(result.id);}
    else {toast(result.message);await refresh();}
  }catch(error){toast(error.message);}finally{state.pending=false;controls();}
}
let closingBatch = null;
document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>{
  if(button.dataset.action!=='cancel'){action(button.dataset.action);return;}
  closingBatch=state.status?.batch?.id;
  $('cancel-summary').textContent=`批次 ${closingBatch?.slice(0,8)} · 已处理 ${state.status.lanes.reduce((n,l)=>n+l.processed,0)} 个账号。结束后可配置下一批。`;
  $('cancel-dialog').showModal();
}));
$('keep-batch').addEventListener('click',()=>$('cancel-dialog').close());
$('confirm-cancel').addEventListener('click',()=>{$('cancel-dialog').close();action('cancel',{batch_id:closingBatch});});
document.querySelectorAll('[data-folder]').forEach(button=>button.addEventListener('click',async()=>{try{toast((await api('folder',{target:button.dataset.folder})).message);}catch(error){toast(error.message);}}));
function addLane(lane){const row=el('div','lane-form-row');const enabled=el('input');enabled.type='checkbox';enabled.checked=true;enabled.setAttribute('aria-label','启用此实例');const index=el('input');index.type='number';index.min=0;index.max=255;index.value=lane.instance;index.required=true;index.setAttribute('aria-label','MuMu 实例编号');const select=el('select');select.setAttribute('aria-label','抽卡预设');for(const preset of state.status.presets){const option=el('option','',`${preset.name} · 实际 ${preset.total_pulls} 抽`);option.value=preset.name;select.append(option);}select.value=lane.preset;const remove=el('button','icon-button','×');remove.type='button';remove.title='移除此实例配置';remove.setAttribute('aria-label','移除此实例配置');remove.addEventListener('click',()=>{row.remove();runSummary();});row.append(enabled,index,select,remove);row.addEventListener('input',runSummary);$('lane-inputs').append(row);}
function runSummary(){const n=[...$('lane-inputs').children].filter(row=>row.querySelector('input[type=checkbox]').checked).length;const count=Number($('run-count').value);$('run-summary').textContent=`${n} 个实例 · ${count===0?'共享持续运行':`共 ${count} 个账号，按空闲实例分配`}`;}
$('add-lane').addEventListener('click',()=>{const used=[...$('lane-inputs').children].map(row=>Number(row.querySelector('input[type=number]').value));let index=0;while(used.includes(index))index++;addLane({instance:index,preset:state.status.presets[0]?.name});runSummary();});$('run-count').addEventListener('input',runSummary);
$('run-form').addEventListener('submit',event=>{event.preventDefault();const lanes=[...$('lane-inputs').children].filter(row=>row.querySelector('input[type=checkbox]').checked).map(row=>({instance:Number(row.querySelector('input[type=number]').value),preset:row.querySelector('select').value}));if(!lanes.length){toast('请至少启用一个实例。');return;}action('start',{run:{lanes,count:Number($('run-count').value),channel:$('channel').value,async_start:$('async-start').checked,debug_mode:$('debug-mode').checked,preset_version:state.status.preset_version}});});
function accountIndex(){const value=$('account-index').value;if(value===''||!Number.isSafeInteger(Number(value))||Number(value)<0)throw new Error('请填写有效的账号 index。');return Number(value);}
const accountQuery=createAccountQuery({api,getIndex:accountIndex,input:$('account-index'),button:$('query-account'),result:$('account-query-result'),summary:$('account-query-summary'),preset:$('account-query-preset'),cards:$('account-query-cards'),el});
$('account-index').addEventListener('input',()=>{$('email-result').hidden=true;});
$('make-preview').addEventListener('click',()=>{try{window.open(serverUrl('account-'+accountIndex()),'_blank','noopener');}catch(error){toast(error.message);}});
$('find-email').addEventListener('click',async()=>{try{accountQuery.ensure();const result=await api('email',{index:accountIndex()});$('email-value').textContent=result.email;$('email-result').hidden=false;}catch(error){$('email-result').hidden=true;toast(error.message);}});
$('copy-email').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('email-value').textContent);toast('邮箱已复制。');}catch{toast('复制失败，请选择邮箱文字手动复制。');}});

$('mumu-check').addEventListener('click',()=>{const field=$('check-instance');if(!field.reportValidity())return;action('mumu_check',{instance:Number(field.value)});});
async function openJob(id){state.selectedJob=id;try{await updateJob();if(!$('job-dialog').open)$('job-dialog').showModal();}catch(error){toast(error.message);}}
async function updateJob(){const job=await api(`jobs/${state.selectedJob}`);$('job-title').textContent=job.title;let summary=`${statuses[job.status]||job.status} · ${duration((job.finished_at||Date.now()/1000)-job.started_at)}`;$('job-summary').textContent=summary;$('job-log').textContent=job.log||'任务已启动，等待输出…';}
$('close-job').addEventListener('click',()=>$('job-dialog').close());$('job-dialog').addEventListener('click',event=>{if(event.target===$('job-dialog')){const r=$('job-dialog').getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)$('job-dialog').close();}});
$('refresh').addEventListener('click',refresh);$('quit').addEventListener('click',async()=>{
  const button=$('quit');
  if(button.disabled)return;
  button.disabled=true;button.textContent='正在关闭…';
  try{
    const result=await api('shutdown',{});
    state.quitting=true;toast(result.message);
    document.querySelectorAll('button,input,select,textarea').forEach(control=>control.disabled=true);
    $('connection').textContent='控制台正在关闭';
    $('notice').hidden=false;
    $('notice').textContent='正在安全收尾并关闭控制台。可以关闭此页面；再次使用时双击“启动控制台.cmd”。';
  }catch(error){button.disabled=false;button.textContent='关闭控制台';toast(error.message);}
});
const laneRecovery=createLaneRecovery({api,el,onMessage:toast,onChange:()=>{if(state.status)renderLanes(state.status);}});


async function loadCloud(){try{const data=await api('cloud');$('service-url').value=data.service_url||'';}catch(error){toast(error.message);}}
$('cloud-settings').addEventListener('submit',async event=>{event.preventDefault();try{await api('cloud',{service_url:$('service-url').value.trim(),credential:$('cloud-credential').value});$('cloud-credential').value='';toast('本机接入配置已加密保存。');}catch(error){toast(error.message);}});
loadCloud();

const accountActions=createAccountActions({
  input:$('account-index'),getIndex:accountIndex,usedButton:$('mark-account-used'),bookedButton:$('toggle-account-booked'),
  dialog:$('account-action-dialog'),title:$('account-action-title'),summary:$('account-action-summary'),warning:$('account-action-warning'),
  progress:$('account-action-progress'),confirm:$('account-action-confirm'),cancel:$('account-action-cancel'),
  preview:index=>api('accounts/usage-preview',{index}),
  write:(value,target)=>api('accounts/set-usage',{index:value.index,token:value.token,target_status:target}),
  isBusy:()=>state.pending,onMessage:toast,
  onChange:async()=>{if(!$('account-query-result').hidden)await accountQuery.query();await refresh();},
});
$('query-account').addEventListener('click',()=>accountActions.refresh());

async function renderUpdates(data){$('update-state').textContent=data.status==='available'?`稳定版 ${data.version} 可用`:data.status==='downloaded'?'已验证，等待安装':data.status==='up_to_date'?'当前已是最新稳定版':data.status==='unavailable'?'检查暂不可用':data.status==='source'?'源码环境使用 Git 更新':'等待更新检查';$('update-download').disabled=!data.available||data.downloaded;$('update-install').disabled=!data.downloaded;}
for(const kind of ['check','download','install'])$('update-'+kind).addEventListener('click',async()=>{try{const data=await api('updates/'+kind,{});if(kind==='install'){state.quitting=true;toast(data.message);}else await renderUpdates(data);}catch(error){toast(error.message);}});
$('debug-details').addEventListener('click',()=>window.open('/api/launcher/debug-report','_blank','noopener'));

$('debug-mode').addEventListener('change',()=>{$('debug-details').hidden=!($('debug-mode').checked||state.status?.batch?.debug_mode);});

window.consoleState.subscribe(receiveState);
window.consoleState.start();
