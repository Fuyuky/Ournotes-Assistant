"use strict";
(() => {
  const el = id => document.getElementById(id);
  const panel = el('email-rebind-panel');
  if (!panel) return;
  const token = document.querySelector('meta[name="launcher-token"]').content;
  const id = () => crypto.randomUUID().replaceAll('-', '');
  let task = null, taskRunning = false, busy = false, requestId = null, confirmingId = null, version = 0, eligibleIndex = null, used = false, eligibilityTimer = null;
  let secretLoading = null, secretText = '';
  let eligibilityMessage = '', eligibilityVersion = 0;
  let targetCommand = null;
  async function call(path, body) {
    const response = await fetch('/api/launcher/email-rebind/' + path, {
      cache: 'no-store', headers: body === undefined ? {} : {'Content-Type':'application/json','X-Launcher-Token':token},
      ...(body === undefined ? {} : {method:'POST', body:JSON.stringify(body)})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '换绑请求未完成。');
    return data;
  }
  function render(value) {
    const prior = task?.id, priorState = task?.state, priorRevision = task?.email_revision;
    task = value.task;
    panel.dataset.state = task?.state || 'idle';
    if (value.running !== undefined) taskRunning = value.running;
    const blocked = task?.state === 'error' && task?.error?.phase === 'preflight';
    const completed = task?.state === 'success' || task?.state === 'ending';
    const changing = task?.state === 'waiting_email';
    if (prior !== task?.id || priorRevision !== task?.email_revision) targetCommand=null;
    if (task?.state === 'success' && String(task.index) === el('account-index').value) {used=true;eligibleIndex=String(task.index);}
    if (prior !== task?.id || !passwordAvailable()) secretText='';
    if (task) {
      panel.hidden = false;
      if (!changing || prior !== task.id || priorState !== 'waiting_email' || priorRevision !== task.email_revision) el('rebind-email').value = task.email || '';
      el('rebind-instance').value = task.instance;
    } else if (prior) {
      el('rebind-email').value=''; requestId=null; confirmingId=null;
    }
    el('rebind-email').disabled = (!!task && !changing) || busy;
    el('rebind-instance').disabled = !!task || busy;
    el('rebind-start').disabled = !!task || busy || used || eligibleIndex !== el('account-index').value;
    el('rebind-confirm').disabled = busy || task?.state !== 'waiting_b';
    el('rebind-target').hidden = !changing;
    el('rebind-target').disabled = busy || !changing;
    el('rebind-stop').disabled = busy || !task || ['stopping','ending'].includes(task.state);
    el('rebind-password').textContent = secretText || (task?.state === 'interrupted' ? '服务已重启，密码已丢失。' : passwordAvailable() ? '正在读取密码文案…' : '开始换绑后显示当前密码。');
    el('rebind-copy').disabled = !secretText;
    if (passwordAvailable() && !secretText && secretLoading !== task.id) showPassword();
    el('rebind-start').textContent = blocked ? '未启动换绑' : task ? ({waiting_email:'等待更换邮箱',waiting_b:'等待邮件确认',checking_b:'正在核验邮件',success:'换绑成功',error:'需要核查',interrupted:'任务已中断',stopping:'停止中',ending:'正在结束',marking_used:'正在标记已使用'}[task.state] || '换绑进行中') : '开始换绑';
    el('rebind-stop').textContent = task?.state === 'ending' ? '结束中…' : completed ? '结束换绑' : blocked && !taskRunning ? '清除任务' : '停止换绑';
    el('rebind-stop').className = completed || blocked && !taskRunning ? 'button secondary' : 'button danger';
    el('rebind-status').textContent = task?.message || eligibilityMessage || (used ? '该账号已使用，不能进行邮箱换绑。' : '开始后自动生成随机密码，清除任务后不保留。');
    el('rebind-start').title = task ? '请先结束或清除当前换绑任务。' : used ? '该账号已使用。' : eligibleIndex !== el('account-index').value ? (eligibilityMessage || '请展开换绑面板或重新输入账号编号以检查资格。') : '';
    el('rebind-identity').textContent = task ? `当前任务：账号 ${task.index} · 实例 ${task.instance}` : '';
    const error = task?.error;
    el('rebind-error-hint').hidden = !error;
    el('rebind-error-hint').textContent = error?.hint || '';
    el('rebind-error-details').hidden = !error;
    const sources = {'runtime-db':'本机运行数据库','runner-status':'调度状态文件','safety-check':'安全校验','account-check':'账号检查',cloud:'云端检查'};
    const details = error ? [`错误编号：${error.code}`, `来源：${sources[error.source] || '启动检查'}`] : [];
    if (error?.exception_type) details.push(`异常类型：${error.exception_type}`);
    if (error?.field) details.push(`缺失字段：${error.field}`);
    for (const [key, label] of [['errno','系统错误码'],['winerror','Windows 错误码'],['sqlite_errorcode','SQLite 错误码'],['http_status','HTTP 状态码']]) {
      if (Number.isInteger(error?.[key])) details.push(`${label}：${error[key]}`);
    }
    if (error?.occurred_at) details.push(`时间：${new Date(error.occurred_at).toLocaleString()}`);
    if (error?.diagnostic_saved === false) details.push('诊断文件未能保存，当前页面仍可查看错误信息');
    el('rebind-error-info').textContent = details.join('；');
  }
  async function action(fn) {
    if (busy) return;
    busy=true; version++; render({task});
    try {render(await fn());} catch(error) {el('rebind-status').textContent=error.message;}
    finally {busy=false; const message=el('rebind-status').textContent; render({task}); el('rebind-status').textContent=message;}
  }
  async function eligibility() {
    const index = el('account-index').value;
    const current = ++eligibilityVersion;
    eligibilityMessage = '正在检查账号是否可换绑…';
    eligibleIndex=null; used=false; render({task});
    if (!index || !Number.isSafeInteger(Number(index)) || Number(index)<0) return;
    try {
      const value=await call('eligibility',{index:Number(index)});
      if(current!==eligibilityVersion || index!==el('account-index').value) return;
      eligibilityMessage = '';
      eligibleIndex=index; used=value.can_rebind === undefined ? value.used : !value.can_rebind; render({task});
      if(used && !task) el('rebind-status').textContent='该账号已使用，不能进行邮箱换绑。';
    } catch(error) {
      if(current!==eligibilityVersion || index!==el('account-index').value) return;
      eligibilityMessage=error.message + ' 可收起并重新展开换绑面板重试。'; render({task});
    }
  }
  el('account-rebind-toggle').onclick = () => {panel.hidden=!panel.hidden;if(!panel.hidden&&!task) eligibility();};
  el('account-index').addEventListener('input',()=>{
    eligibleIndex=null; used=false; render({task}); clearTimeout(eligibilityTimer);
    if(!panel.hidden&&!task) eligibilityTimer=setTimeout(eligibility,250);
  });
  el('account-index').addEventListener('change',()=>{if(!panel.hidden&&!task) eligibility();});
  el('rebind-start').onclick = () => action(async () => {
    const index=Number(el('account-index').value), instance=Number(el('rebind-instance').value), email=el('rebind-email').value.trim();
    if (!el('account-index').value || !Number.isSafeInteger(index) || index<0 || !Number.isInteger(instance) || instance<0 || !el('rebind-email').checkValidity() || !email) throw new Error('请填写有效账号编号、实例编号和目标邮箱。');
    requestId ||= id();
    return call('start',{index,instance,email,request_id:requestId});
  });
  el('rebind-confirm').onclick = () => action(async () => {
    confirmingId ||= id();
    const value=await call('confirmed',{task_id:task.id,command_id:confirmingId});
    confirmingId=null; return value;
  });
  el('rebind-target').onclick = () => action(async () => {
    const email=el('rebind-email').value.trim();
    if (!email || !el('rebind-email').checkValidity()) throw new Error('请填写完整有效的目标邮箱。');
    if (!targetCommand || targetCommand.email!==email) targetCommand={email,command_id:id()};
    return call('target',{task_id:task.id,email_revision:task.email_revision,...targetCommand});
  });
  el('rebind-stop').onclick = async () => {
    await action(() => call('stop',{task_id:task.id,command_id:id()}));
    if (!task) await eligibility();
  };
  function passwordAvailable() {
    return !!task && !['interrupted','stopping','ending'].includes(task.state);
  }
  async function password() {
    const current=task?.id;
    if (!passwordAvailable()) return '';
    const result=await call('password',{task_id:current,command_id:id()});
    if (task?.id!==current || !passwordAvailable()) return '';
    return result.password;
  }
  async function showPassword() {
    const current=task?.id;
    if (!passwordAvailable() || secretLoading === current) return;
    secretLoading=current;
    try {
      const value=await password();
      if(value) {secretText=`好啦，当前密码是 ${value} ~`; el('rebind-password').textContent=secretText; el('rebind-copy').disabled=false;}
    } catch {
      if (task?.id===current && passwordAvailable()) el('rebind-password').textContent='密码文案读取失败，正在等待重新读取…';
    } finally {if(secretLoading===current) secretLoading=null;}
  }
  async function copyText(field) {
    // Use already-loaded text during the click gesture; no network await before clipboard access.
    try {await navigator.clipboard.writeText(field.textContent);}
    catch {
      const range=document.createRange(); range.selectNodeContents(field);
      const selection=document.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      field.focus(); el('rebind-status').textContent='已选中文案，请按 Ctrl+C 复制。';
    }
  }
  el('rebind-copy-sent').onclick = () => copyText(el('rebind-message-sent'));
  el('rebind-copy-receipt').onclick = () => copyText(el('rebind-message-receipt'));
  el('rebind-copy').onclick = () => {if(secretText) return copyText(el('rebind-password'));};
  window.consoleState.subscribe(snapshot => {
    if (!busy && snapshot.email_rebind) render(snapshot.email_rebind);
  });
})();
