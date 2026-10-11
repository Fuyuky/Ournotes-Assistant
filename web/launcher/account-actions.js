"use strict";

function createAccountActions(options) {
  const {input, getIndex, usedButton, bookedButton, dialog, title, summary, warning, progress, confirm, cancel} = options;
  const states = {available:'未使用', booked:'已拍下', used:'已使用'};
  const actions = {available:'标记未拍下', booked:'标记已拍下', used:'标记已使用'};
  let latest = null, loading = false, lookupSerial = 0, lookupTimer;
  let phase = 'idle', dialogSerial = 0, index, target, operation, plan;
  const active = () => phase !== 'idle';
  const writing = () => phase === 'writing' || phase === 'checking';
  function selectedIndex() {try {return getIndex();} catch {return null;}}
  function validate(value, number) {
    if (value.index !== number) throw new Error('返回的账号编号不一致，请重新查询。');
    if (value.usage_protocol !== 2 || !Object.hasOwn(states, value.usage_status)) throw new Error('请先升级云端账号状态服务。');
    return value;
  }
  function update(value) {
    if (value.index !== selectedIndex() || (latest?.index === value.index && latest.version > value.version)) return;
    latest = value;
    render();
  }
  function render() {
    const number = selectedIndex(), value = latest?.index === number ? latest : null;
    bookedButton.textContent = value?.usage_status === 'booked' ? '标记未拍下' : '标记已拍下';
    usedButton.disabled = bookedButton.disabled = number === null || loading || active() || options.isBusy() || value?.usage_status === 'used';
    const waiting = ['reading','writing','checking'].includes(phase);
    dialog.setAttribute('aria-busy', String(waiting));
    confirm.disabled = waiting || (phase === 'ready' && plan?.usage_status === 'used');
    cancel.disabled = writing();
    confirm.classList.toggle('is-loading', waiting);
    confirm.textContent = phase === 'reading' ? '正在查询…' : phase === 'writing' ? '正在标记…' : phase === 'checking' ? '正在核对…'
      : ['unknown','error'].includes(phase) ? '重新查询' : plan?.usage_status === 'used' ? '已使用' : '确认' + (actions[target] || '标记');
    if (target) {
      title.textContent = actions[target];
      warning.textContent = target === 'used' ? '确认标记已使用后不可撤销；此账号将禁止换绑。'
        : target === 'available' ? '确认后撤销已拍下标记；现有换绑及核查保护继续保留。'
        : '已拍下可以撤销；服务器网页显示为已使用。';
    }
  }
  function show(value, text) {
    phase = value;
    progress.textContent = text;
    render();
  }
  async function refresh() {
    const request = ++lookupSerial, number = selectedIndex();
    if (number === null) {loading = false; render(); return;}
    loading = true; render();
    try {
      const value = validate(await options.preview(number), number);
      if (request === lookupSerial) update(value);
    } catch {
      // A button click can retry the authoritative read in its confirmation window.
    } finally {
      if (request === lookupSerial) {loading = false; render();}
    }
  }
  async function read() {
    const request = ++dialogSerial;
    plan = null;
    show('reading', '正在核对云端账号…');
    try {
      const value = validate(await options.preview(index), index);
      if (request !== dialogSerial || !dialog.open) return;
      plan = value;
      target = operation === 'used' ? 'used' : value.usage_status === 'booked' ? 'available' : 'booked';
      update(value);
      summary.textContent = `账号 ${index} · 当前${states[value.usage_status]}`;
      show('ready', value.usage_status === 'used' ? '此账号已永久使用，不能更改标记。' : `请确认${actions[target]}。`);
    } catch (error) {
      if (request === dialogSerial && dialog.open) show('error', error.message);
    }
  }
  async function open(action) {
    if (active() || options.isBusy()) return;
    index = selectedIndex();
    if (index === null) return;
    operation = action;
    target = action === 'used' ? 'used' : latest?.usage_status === 'booked' ? 'available' : 'booked';
    summary.textContent = `账号 ${index}`;
    dialog.showModal();
    await read();
  }
  async function succeeded(message) {
    dialog.close();
    options.onMessage(message);
    await refresh();
    try {await options.onChange();} catch {options.onMessage('标记已确认，请刷新页面查看。');}
  }
  async function submit() {
    if (phase !== 'ready' || !plan || plan.usage_status === 'used') return;
    const request = ++dialogSerial, submittedTarget = target, baseVersion = plan.version;
    show('writing', '正在提交，请等待结果。');
    try {
      const result = await options.write(plan, submittedTarget);
      if (request !== dialogSerial) return;
      await succeeded(result.message);
    } catch (error) {
      if (request !== dialogSerial) return;
      show('checking', '正在核对云端结果，暂不重复提交…');
      try {
        const value = validate(await options.preview(index), index);
        if (request !== dialogSerial) return;
        update(value);
        if (value.usage_status === submittedTarget && value.version > baseVersion) {
          await succeeded(`已核对云端：账号 ${index} 当前${states[submittedTarget]}。`);
          return;
        }
        plan = value;
        summary.textContent = `账号 ${index} · 当前${states[value.usage_status]}`;
        show('ready', `${error.message} 已读取最新状态，请重新确认。`);
      } catch {
        if (request === dialogSerial) show('unknown', '操作结果尚未确认，请先重新查询。');
      }
    }
  }
  usedButton.addEventListener('click', () => {if (!usedButton.disabled) open('used');});
  bookedButton.addEventListener('click', () => {if (!bookedButton.disabled) open('booking');});
  confirm.addEventListener('click', () => {if (['unknown','error'].includes(phase)) read(); else submit();});
  cancel.addEventListener('click', () => {if (!writing()) dialog.close();});
  dialog.addEventListener('cancel', event => {if (writing()) event.preventDefault();});
  dialog.addEventListener('close', () => {dialogSerial++; plan = null; show('idle', '');});
  input.addEventListener('input', () => {
    lookupSerial++; latest = null; loading = true; render(); clearTimeout(lookupTimer);
    lookupTimer = setTimeout(refresh, 250);
  });
  render();
  return {refresh, render};
}
