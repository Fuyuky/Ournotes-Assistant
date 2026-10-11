"use strict";
const $ = id => document.getElementById(id);
function node(tag, text, className) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (className) n.className = className;
  return n;
}
function notice(message) { $('notice').textContent = message; $('notice').hidden = !message; }
let session = null, offset = 0, total = 0, selected = null, credential = null, socket = null;
let presetDocument = null, activityDocument = null, announcementVersion = null;
let reportRequest = 0, accountsRequest = 0;

async function api(path, body, method = body === undefined ? 'GET' : 'POST') {
  const response = await fetch('/admin/api/' + path, {method, credentials: 'same-origin', cache: 'no-store',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== 'login') loggedIn(null);
    const messages = {409: '资料已更新，请刷新后再操作。', 403: '此操作没有权限。',
      404: '没有找到完整成功的账号或指定资料。', 503: '服务暂时不可用，请稍后重试。',
      424: 'Gmail 授权需要通过独立工具更新。', 422: '输入无效，请检查填写内容。'};
    throw new Error(value.detail || messages[response.status] || '暂时无法读取，请稍后重试。');
  }
  return value;
}
function loggedIn(value) {
  session = value;
  $('login-panel').hidden = !!value; $('workspace').hidden = !value; $('logout').hidden = !value;
  $('admins-tab').hidden = !value?.is_superadmin;
  $('identity').textContent = value ? value.username + (value.is_superadmin ? ' · 超级管理员' : ' · 管理员') : '';
  if (!value) {
    socket?.close(); socket = null; selected = credential = null;
    $('credential').textContent = ''; $('credential').hidden = true; $('credential-copy').hidden = true;
    $('tables').replaceChildren(); $('account-list').replaceChildren(); $('account-detail').replaceChildren();
  }
}
function button(text, handler, className = 'secondary') {
  const n = node('button', text, className); n.type = 'button';
  n.addEventListener('click', async () => {n.disabled = true; try {await handler(); notice('');}
    catch (error) {notice(error.message);} finally {n.disabled = !!(n.dataset.completed || n.dataset.expired);}});
  return n;
}
function time(value) {return new Date(value * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'});}

async function owners() {
  const value = await api('administrators');
  for (const id of ['owner', 'statistics-owner']) {
    const prior = $(id).value; $(id).replaceChildren(new Option('全部用户', ''));
    for (const user of value.administrators) $(id).append(new Option(user.username + (user.enabled ? '' : '（已停用）'), user.username));
    $(id).value = prior;
  }
  if (!session.is_superadmin) return;
  $('administrator-list').replaceChildren();
  for (const user of value.administrators) {
    const row = node('div', undefined, 'administrator-row');
    row.append(node('strong', user.username), node('span', user.enabled ? '有效' : '已停用', 'muted'));
    if (user.username !== 'Fuyuki' && user.enabled) row.append(button('删除管理员', async () => {
      if (!window.confirm(`删除 ${user.username} 后，其登录和接入均会失效。历史成果保留。`)) return;
      await api('administrators/' + encodeURIComponent(user.username), undefined, 'DELETE'); await owners();
    }, 'danger'));
    $('administrator-list').append(row);
  }
}
async function accounts() {
  const request = ++accountsRequest;
  const index = $('account-index').value.trim();
  if (index && (!/^\d+$/.test(index) || !Number.isSafeInteger(Number(index)))) throw new Error('请输入有效编号。');
  const query = new URLSearchParams({offset: String(offset), limit: '50'});
  if (index) query.set('index', index);
  if ($('owner').value) query.set('owner_username', $('owner').value);
  const source = await api('accounts?' + query);
  if (request !== accountsRequest) return;
  total = source.total; $('account-count').textContent = `${total.toLocaleString()} 个完整成功成果`;
  const fragment = document.createDocumentFragment();
  for (const value of source.accounts) {
    const row = node('article', undefined, 'account-row');
    const title = node('a', '#' + value.result.index); title.href = '#account-' + value.result.index;
    row.append(title, node('span', value.owner_username, 'muted'),
      node('span', `${value.projection.ssr_total} SSR`), node('span', value.result.used ? '已使用' : '未使用'));
    if (value.projection.activity) row.append(node('span', `${value.projection.activity.label} UP ${value.projection.activity.up_count}/${value.projection.activity.up_total}`, 'activity-tag'));
    fragment.append(row);
  }
  if (!source.accounts.length) fragment.append(node('p', '没有符合条件的完整成果。', 'muted'));
  $('account-list').replaceChildren(fragment); $('page').textContent = `${Math.floor(offset / 50) + 1} / ${Math.max(1, Math.ceil(total / 50))}`;
  $('previous').disabled = offset === 0; $('next').disabled = offset + 50 >= total;
}
async function detail(index) {
  const source = await api('accounts?index=' + index);
  selected = source.accounts[0];
  if (!selected) throw new Error('没有找到完整成功的账号。');
  const value = selected.result, box = $('account-detail'); box.hidden = false; box.replaceChildren();
  const heading = node('div', undefined, 'heading'); heading.append(node('h2', '账号 #' + value.index),
    button('打印展示', () => window.print())); box.append(heading);
  box.append(node('p', `上传用户：${selected.owner_username} · ${value.pull_count} 抽 · ${selected.projection.ssr_total} SSR`, 'muted'),
    node('p', value.email, 'private-email'));
  if (selected.projection.activity) box.append(node('p', `${selected.projection.activity.label} UP ${selected.projection.activity.up_count}/${selected.projection.activity.up_total}`, 'activity-tag'));
  const history = node('details'); history.append(node('summary', '历史抽卡计划'));
  if (value.preset_snapshot) {
    history.append(node('p', `${value.preset_snapshot.key} · 版本 ${value.preset_snapshot.version ?? '历史未记录'}`));
    for (const entry of value.preset_snapshot.plan) history.append(node('p', `${entry.pool_key} · ${entry.resource === 'stars' ? '星石' : '券'} · ${entry.pulls} 抽`));
  } else history.append(node('p', (value.pool_keys || []).join('、') || '旧成果未保存计划归属。'));
  if (value.draw_labels.length) history.append(node('p', value.draw_labels.join('、'))); box.append(history);
  const controls = node('div', undefined, 'actions');
  controls.append(node('span', value.used ? '已使用' : '未使用', 'usage-label'));
  if (selected.usage_status !== 'used') controls.append(button('标记已使用', async () => {
    if (!window.confirm('标记已使用后不能撤销。')) return;
    await api(`accounts/${value.index}/usage`, {request_id: crypto.randomUUID(), expected_version: selected.version, target_status: 'used'});
    await detail(value.index); await accounts();
  }));
  box.append(controls);
  const cards = node('div', undefined, 'ssr-cards');
  for (const card of selected.ssr_cards) {
    const figure = node('figure'); const img = node('img'); img.src = `/admin/api/art/${card.kind}/${card.id}`;
    img.alt = `${card.kind === 'member' ? '角色卡' : '支援卡'} ${card.title} ${card.name}`; img.loading = 'lazy';
    figure.append(img, node('figcaption', `${card.name} · ${card.kind === 'member' ? '角色卡' : '支援卡'} ×${card.count}`)); cards.append(figure);
  }
  box.append(cards);
  const mail = node('section', undefined, 'mail-panel'); mail.append(node('h3', '最新邮件'));
  const options = node('select'); options.append(new Option('最近 10 分钟', '10'), new Option('最近 24 小时', '1440'));
  const messages = node('div');
  mail.append(options, button('读取邮件', async () => {
    const response = await api(`mail?index=${value.index}&minutes=${options.value}`); messages.replaceChildren();
    if (!response.messages.length) messages.append(node('p', '此时间范围没有匹配邮件。', 'muted'));
    for (const message of response.messages) {
      const row = node('div', undefined, 'mail-message');
      row.append(node('strong', message.purpose === 'login' ? '登录验证码' : '原邮箱验证'), node('p', time(message.received_at) + '（北京时间）', 'muted'));
      if (message.code) row.append(node('code', message.code));
      if (message.purpose === 'identity') {
        const execute = button(message.execution_status ? '已执行：' + ({done:'成功', rejected:'拒绝', uncertain:'结果待核查'}[message.execution_status] || message.execution_status) : '执行验证链接', async () => {
          const result = await api(`mail/${value.index}/execute`, {message_id: message.message_id, request_id: crypto.randomUUID()});
          execute.textContent = {done:'验证成功', rejected:'验证被拒绝', uncertain:'验证结果待核查'}[result.status] || result.status;
          execute.dataset.completed = 'yes'; execute.disabled = true;
        });
        execute.disabled = !message.can_execute;
        // The server rechecks the real receipt time before execution.
        if (message.can_execute) setTimeout(() => {execute.dataset.expired = 'yes'; execute.disabled = true; execute.title = '邮件已超过 10 分钟';}, Math.max(0, (message.received_at + 600 - response.observed_at) * 1000));
        row.append(execute); if (!message.can_execute && !message.execution_status) row.append(node('p', '邮件超过 10 分钟，验证按钮已禁用。', 'muted'));
      }
      messages.append(row);
    }
  }), messages, node('p', '读取邮件不会执行验证；标记已使用是独立操作。', 'muted small'));
  box.append(mail); box.scrollIntoView({block: 'start', behavior: 'smooth'});
}
async function statistics() {
  const request = ++reportRequest; $('refresh').disabled = true;
  try {
    const report = await api('statistics?scope=' + $('scope').value + '&owner_username=' + encodeURIComponent($('statistics-owner').value || ''));
    if (request !== reportRequest) return;
    const fragment = document.createDocumentFragment();
    for (const metric of report.metrics) {
      const card = node('article', undefined, 'metric'); card.append(node('h3', metric.label));
      const summary = node('p', undefined, 'metric-summary'); summary.append(node('strong', metric.max ?? '—'), node('span', `最高 · 平均 ${metric.mean === null ? '—' : metric.mean.toFixed(2)}`)); card.append(summary);
      const table = node('table'), head = node('tr'), thead = node('thead'), tbody = node('tbody');
      for (const text of ['数量', '账号数', '占比', '分布']) head.append(node('th', text)); thead.append(head); table.append(thead);
      for (const bin of metric.histogram) {
        const row = node('tr'); row.append(node('td', bin.value), node('td', bin.count), node('td', bin.percent.toFixed(1) + '%'));
        const cell = node('td'), bar = node('progress'); bar.max = 100; bar.value = bin.percent; cell.append(bar); row.append(cell); tbody.append(row);
      }
      table.append(tbody); card.append(table);
      if (!metric.histogram.length) card.append(node('p', '暂无完整账号。', 'muted'));
      if (metric.indices.length) {const d = node('details'); d.append(node('summary', `最高记录编号 · ${metric.indices.length} 个账号`), node('p', metric.indices.join('、'))); card.append(d);}
      fragment.append(card);
    }
    $('tables').replaceChildren(fragment); $('sample').textContent = `${report.n.toLocaleString()} 个完整样本`;
    $('observed').textContent = '读取时间：' + time(report.observed_at) + '（北京时间）'; notice('');
  } catch (error) {notice(error.message);} finally {if (request === reportRequest) $('refresh').disabled = false;}
}
async function activities() {
  activityDocument = await api('activities'); $('activity-select').replaceChildren(new Option('无活动', ''));
  for (const value of activityDocument.activities) $('activity-select').append(new Option(`${value.label} · #${value.gacha_id}`, value.id));
  $('activity-select').value = activityDocument.active_activity_id || ''; activityDetails();
}
function activityDetails() {
  const value = activityDocument?.activities.find(v => v.id === $('activity-select').value);
  $('activity-detail').textContent = value ? `${value.name} · ${value.pickup_card_keys.length} 种 UP（角色卡与支援卡分别计数）` : '新分配账号不标记活动。';
}
function entryFields(value = {}) {
  const row = node('div', undefined, 'preset-entry');
  const pool = node('input'); pool.value = value.pool_key || ''; pool.placeholder = '卡池标识'; pool.required = true; pool.dataset.field = 'pool_key';
  const resource = node('select'); resource.append(new Option('星石', 'stars'), new Option('券', 'ticket')); resource.value = value.resource || 'stars'; resource.dataset.field = 'resource';
  const pulls = node('input'); pulls.type = 'number'; pulls.min = '1'; pulls.max = '1000'; pulls.value = value.pulls || 10; pulls.required = true; pulls.dataset.field = 'pulls'; pulls.setAttribute('aria-label', '抽数');
  const targets = node('input'); targets.value = (value.until_owned || []).join(','); targets.placeholder = '集齐转池目标（可留空）'; targets.dataset.field = 'until_owned';
  const fallback = node('input'); fallback.value = value.fallback_pool_key || ''; fallback.placeholder = '转入卡池（可留空）'; fallback.dataset.field = 'fallback_pool_key';
  row.append(pool, resource, pulls, targets, fallback, button('移除', () => row.remove())); $('preset-entries').append(row);
}
function editPreset(key) {
  const value = presetDocument?.presets[key]; $('preset-key').value = key || ''; $('preset-key').readOnly = !!value;
  $('preset-name').value = value?.name || ''; $('preset-description').value = value?.description || '';
  $('preset-entries').replaceChildren(); for (const entry of value?.entries || [{}]) entryFields(entry);
  $('preset-delete').disabled = !value;
}
async function presets() {
  presetDocument = await api('presets'); const prior = $('preset-select').value; $('preset-select').replaceChildren();
  for (const [key, value] of Object.entries(presetDocument.presets)) $('preset-select').append(new Option(value.name, key));
  if (presetDocument.presets[prior]) $('preset-select').value = prior;
  editPreset($('preset-select').value);
}
async function route() {
  if (!session) return;
  const hash = location.hash.slice(1) || 'accounts', match = /^account-(\d+)$/.exec(hash);
  let view = match ? 'accounts' : hash;
  if (!['accounts', 'statistics', 'presets', 'activities', 'announcement', 'connection', 'administrators'].includes(view) || (view === 'administrators' && !session.is_superadmin)) view = 'accounts';
  for (const panel of document.querySelectorAll('[data-view]')) panel.hidden = panel.dataset.view !== view;
  for (const link of $('tabs').querySelectorAll('a')) {if (link.hash === '#' + view) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');}
  try {
    if (view === 'accounts') {await accounts(); if (match) await detail(Number(match[1])); else $('account-detail').hidden = true;}
    if (view === 'statistics') await statistics();
    if (view === 'presets') await presets();
    if (view === 'activities') await activities();
    if (view === 'announcement') {const value = await api('announcement'); announcementVersion = value.version; $('announcement-content').value = value.content;}
    if (view === 'administrators') await owners();
    if (view === 'connection') {const health = await api('gmail/health'); $('gmail-health').textContent = {configured:'授权已配置', authorization_required:'授权需要更新', not_configured:'尚未配置', not_authorized:'尚未授权'}[health.state] || health.state;}
    notice('');
  } catch (error) {notice(error.message);}
}
function events() {
  socket?.close(); socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/admin/api/events`);
  socket.onmessage = event => {try {const value = JSON.parse(event.data); if (value.type === 'results.changed' && !selected) accounts().catch(e => notice(e.message));} catch {}};
  socket.onclose = event => {if (event.code === 4403) loggedIn(null); else if (session) setTimeout(events, 5000);};
}
async function start(value) {loggedIn(value); await owners(); await route(); events();}
function form(id, action) {
  $(id).addEventListener('submit', async event => {event.preventDefault(); const submit = event.currentTarget.querySelector('button[type="submit"]');
    if (submit) submit.disabled = true; try {await action(event.currentTarget); notice('');} catch (error) {notice(error.message);} finally {if (submit) submit.disabled = false;}});
}
form('login-form', async f => {const value = await api('login', {username: f.elements.username.value, password: f.elements.password.value}); f.elements.password.value = ''; await start(value);});
form('accounts-form', async () => {offset = 0; await accounts();});
form('activity-form', async () => {activityDocument = await api('activities/active', {activity_id: $('activity-select').value || null, expected_version: activityDocument.version}); $('activity-status').textContent = '活动已保存。';});
form('announcement-form', async () => {const value = await api('announcement', {content: $('announcement-content').value, expected_version: announcementVersion}); announcementVersion = value.version; $('announcement-status').textContent = value.message;});
form('administrator-form', async f => {await api('administrators', {username: f.elements.username.value, password: f.elements.password.value}); f.reset(); await owners();});
form('preset-form', async () => {
  const entries = [...$('preset-entries').children].map(row => {
    const read = key => row.querySelector(`[data-field="${key}"]`).value.trim();
    const result = {pool_key: read('pool_key'), resource: read('resource'), pulls: Number(read('pulls'))};
    if (read('until_owned') || read('fallback_pool_key')) {result.until_owned = read('until_owned').split(',').map(v => v.trim()).filter(Boolean); result.fallback_pool_key = read('fallback_pool_key');} return result;
  });
  await api('presets/' + encodeURIComponent($('preset-key').value), {expected_version: presetDocument.version, preset: {name: $('preset-name').value, description: $('preset-description').value, entries}}, 'PUT');
  await presets(); $('preset-status').textContent = '预设已保存。';
});
$('logout').addEventListener('click', async () => {try {await api('logout', {}); loggedIn(null);} catch (error) {notice(error.message);}});
$('accounts-refresh').addEventListener('click', () => accounts().catch(e => notice(e.message)));
$('previous').addEventListener('click', () => {offset = Math.max(0, offset - 50); accounts().catch(e => notice(e.message));});
$('next').addEventListener('click', () => {offset += 50; accounts().catch(e => notice(e.message));});
$('scope').addEventListener('change', statistics); $('statistics-owner').addEventListener('change', statistics); $('refresh').addEventListener('click', statistics);
$('activity-select').addEventListener('change', activityDetails); $('preset-select').addEventListener('change', () => editPreset($('preset-select').value));
$('preset-new').addEventListener('click', () => editPreset('')); $('preset-add-entry').addEventListener('click', () => entryFields());
$('preset-delete').addEventListener('click', async () => {if (!window.confirm('删除此共享预设？已开始的运行不受影响。')) return; try {await api('presets/' + encodeURIComponent($('preset-key').value) + '?expected_version=' + presetDocument.version, undefined, 'DELETE'); await presets();} catch (error) {notice(error.message);}});
function showCredential(value) {credential = value; $('credential').textContent = value.token; $('credential').hidden = false; $('credential-copy').hidden = false;}
$('credential-show').addEventListener('click', async () => {try {showCredential(await api('credential'));} catch (error) {notice(error.message);}});
$('credential-copy').addEventListener('click', async () => {try {await navigator.clipboard.writeText(credential.token); $('credential-status').textContent = '凭据已复制。';} catch {$('credential-status').textContent = '复制失败，请手动选择凭据。';}});
$('credential-reset').addEventListener('click', async () => {if (!window.confirm('重置后，本人的全部旧接入和连接都会失效。')) return; try {showCredential(await api('credential/reset', {})); $('credential-status').textContent = '凭据已重置，请在各客户端更新。';} catch (error) {notice(error.message);}});
window.addEventListener('hashchange', route);
(async () => {try {await start(await api('session'));} catch {loggedIn(null);}})();
