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
let reportRequest = 0, accountsRequest = 0, detailRequest = 0;
let accountsLoading = false, presetKey = null, presetsBusy = false;
const accountPageSize = 20;

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
    accountsRequest++; detailRequest++; offset = total = 0; accountsBusy(false);
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
  if (!session?.is_superadmin) return;
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
function accountsBusy(value) {
  accountsLoading = value;
  $('account-list').setAttribute('aria-busy', String(value));
  $('accounts-loading').textContent = value ? '正在读取账号…' : '';
  for (const id of ['accounts-refresh', 'accounts-submit', 'account-index', 'owner', 'account-preset']) $(id).disabled = value;
  $('previous').disabled = value || offset === 0;
  $('next').disabled = value || offset + accountPageSize >= total;
}
function accountPresets(values, id = 'account-preset') {
  const select = $(id), prior = select.value;
  select.replaceChildren(new Option('全部预设', ''));
  for (const value of values) {
    const label = value.id === '__unknown__' ? value.name : `#${value.id} · ${value.name}${value.deleted ? '（已删除）' : ''}`;
    select.append(new Option(`${label} · ${value.count} 个账号`, value.id));
  }
  if (prior && ![...select.options].some(option => option.value === prior)) {
    select.append(new Option(prior === '__unknown__' ? '历史未记录/未匹配' : `预设 #${prior}（暂无完整成果）`, prior));
  }
  select.value = prior;
}
async function accounts(requestedOffset = offset) {
  if (accountsLoading) return;
  const index = $('account-index').value.trim();
  if (index && (!/^\d+$/.test(index) || !Number.isSafeInteger(Number(index)))) throw new Error('请输入有效编号。');
  const request = ++accountsRequest;
  const query = new URLSearchParams({offset: String(requestedOffset), limit: String(accountPageSize)});
  if (index) query.set('index', index);
  if ($('owner').value) query.set('owner_username', $('owner').value);
  if ($('account-preset').value !== '') query.set('preset_id', $('account-preset').value);
  accountsBusy(true);
  try {
    const source = await api('accounts?' + query);
    if (request !== accountsRequest) return;
    offset = source.offset ?? requestedOffset; total = source.total;
    $('account-count').textContent = `${total.toLocaleString()} 个完整成功成果`;
    accountPresets(source.preset_options || []);
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
    $('account-list').replaceChildren(fragment);
    $('page').textContent = `${Math.floor(offset / accountPageSize) + 1} / ${Math.max(1, Math.ceil(total / accountPageSize))}`;
    notice('');
  } finally {
    if (request === accountsRequest) accountsBusy(false);
  }
}
async function detail(index) {
  const request = ++detailRequest;
  const [source, catalog] = await Promise.all([api('accounts?index=' + index),
    presetDocument ? Promise.resolve(presetDocument) : api('presets').catch(() => null)]);
  if (request !== detailRequest) return;
  if (catalog && !presetDocument) presetDocument = catalog;
  selected = source.accounts[0];
  if (!selected) throw new Error('没有找到完整成功的账号。');
  const value = selected.result, box = $('account-detail'); box.hidden = false; box.replaceChildren();
  const heading = node('div', undefined, 'heading'); heading.append(node('h2', '账号 #' + value.index),
    button('打印展示', () => window.print())); box.append(heading);
  box.append(node('p', `上传用户：${selected.owner_username} · ${value.pull_count} 抽 · ${selected.projection.ssr_total} SSR`, 'muted'),
    node('p', value.email, 'private-email'));
  if (selected.projection.activity) box.append(node('p', `${selected.projection.activity.label} UP ${selected.projection.activity.up_count}/${selected.projection.activity.up_total}`, 'activity-tag'));
  const history = node('details'); history.append(node('summary', '历史抽卡计划'));
  const poolName = key => presetPool(key)?.name || '历史卡池';
  if (value.preset_snapshot) {
    const key = String(value.preset_snapshot.key), known = presetDocument?.presets[key], historical = selected.historical_preset;
    const id = historical?.id ?? null;
    const name = historical?.name || known?.name || (!/^[a-z0-9-]+$/i.test(key) ? key : '');
    const label = id !== null ? `预设 #${id}${historical?.deleted ? '（已删除）' : ''}` : '历史预设';
    history.append(node('p', `${label}${name ? ' · ' + name : ''} · 版本 ${value.preset_snapshot.version ?? '历史未记录'}`));
    for (const entry of value.preset_snapshot.plan || []) history.append(node('p', `${poolName(entry.pool_key)} · ${entry.resource === 'stars' ? '免费星钻' : '招募券'} · ${entry.pulls} 抽`));
  } else history.append(node('p', (value.pool_keys || []).map(poolName).join('、') || '旧成果未保存计划归属。'));
  if (value.draw_labels?.length) history.append(node('p', value.draw_labels.join('、'))); box.append(history);
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
  }), messages);
  box.append(mail); box.scrollIntoView({block: 'start', behavior: 'smooth'});
}
async function statistics() {
  const request = ++reportRequest; $('refresh').disabled = true;
  try {
    const params = new URLSearchParams({scope: $('scope').value, owner_username: $('statistics-owner').value || ''});
    if ($('statistics-preset').value) params.set('preset_id', $('statistics-preset').value);
    const report = await api('statistics?' + params);
    if (request !== reportRequest) return;
    accountPresets(report.preset_options || [], 'statistics-preset');
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
      if (metric.indices.length) {const d = node('details'); d.append(node('summary', '最高记录编号'), node('p', metric.indices.join('、'))); card.append(d);}
      fragment.append(card);
    }
    $('tables').replaceChildren(fragment); $('sample').textContent = `${report.n.toLocaleString()} 个账号`;
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
  $('activity-detail').textContent = value ? `${value.name} · ${value.pickup_card_keys.length} 种 UP` : '无活动';
}
const presetPool = key => presetDocument?.pools.find(value => value.key === key || `bdon-gacha-${value.gacha_id}` === key);
function field(label, input) {const result = node('label', label); result.append(input); return result;}
function entryControls() {
  const rows = [...$('preset-entries').children];
  rows.forEach((row, i) => {
    const remove = row.querySelector('button'); remove.disabled = rows.length === 1;
    remove.setAttribute('aria-label', `移除项目 ${i + 1}`);
  });
  $('preset-add-entry').disabled = rows.length >= 64 || !presetDocument?.pools.some(value => value.supported && value.resources.length);
}
function entryFields(value = {}) {
  const row = node('div', undefined, 'preset-entry');
  const pool = node('select'); pool.required = true; pool.dataset.field = 'pool_key';
  for (const entry of presetDocument.pools) {
    if ((!entry.supported || !entry.resources.length) && entry.key !== presetPool(value.pool_key)?.key) continue;
    const option = new Option(`${entry.name} · #${entry.gacha_id}${entry.supported ? '' : '（暂不支持）'}`, entry.key);
    option.disabled = !entry.supported || !entry.resources.length; pool.append(option);
  }
  const chosen = presetPool(value.pool_key);
  if (value.pool_key && !chosen) {
    const option = new Option(`未知卡池：${value.pool_key}`, value.pool_key); option.disabled = true; pool.append(option);
  }
  if (!pool.options.length) pool.append(new Option('没有可用卡池', ''));
  if (value.pool_key) pool.value = chosen?.key || value.pool_key;
  const resource = node('select'); resource.required = true; resource.dataset.field = 'resource';
  function resources(prior) {
    const values = presetPool(pool.value)?.resources || [];
    resource.replaceChildren(...values.map(key => new Option(key === 'stars' ? '免费星钻' : '招募券', key)));
    if (prior && !values.includes(prior)) {const option = new Option('资源待核验', prior); option.disabled = true; resource.append(option);}
    if (!resource.options.length) resource.append(new Option('没有可用资源', ''));
    resource.value = prior || values[0] || '';
  }
  resources(value.resource);
  pool.addEventListener('change', () => resources());
  const pulls = node('input'); pulls.type = 'number'; pulls.min = '1'; pulls.max = '1000'; pulls.step = '1';
  pulls.value = value.pulls ?? 10; pulls.required = true; pulls.dataset.field = 'pulls';
  row.append(field('卡池', pool), field('资源', resource), field('抽数', pulls), button('移除', () => {row.remove(); entryControls();}));
  $('preset-entries').append(row); entryControls();
}
function presetBusy(value) {
  presetsBusy = value; $('preset-fields').disabled = value;
  $('preset-select').disabled = value; $('preset-new').disabled = value || !presetDocument?.pools.some(pool => pool.supported && pool.resources.length);
  $('preset-delete').disabled = value || presetKey === null;
  $('preset-form').setAttribute('aria-busy', String(value));
}
function editPreset(key) {
  const value = presetDocument?.presets[key]; presetKey = value ? String(key) : null;
  $('preset-name').value = value?.name || ''; $('preset-description').value = value?.description || '';
  $('preset-name').setCustomValidity(''); $('preset-status').textContent = '';
  $('preset-select').value = presetKey ?? '';
  $('preset-entries').replaceChildren(); for (const entry of value?.entries || [{}]) entryFields(entry);
  $('preset-delete').disabled = presetsBusy || presetKey === null;
}
function showPresets(value, preferredKey = $('preset-select').value) {
  presetDocument = value; $('preset-select').replaceChildren();
  for (const [key, preset] of Object.entries(value.presets).sort(([a], [b]) => Number(a) - Number(b))) $('preset-select').append(new Option(`#${key} · ${preset.name}`, key));
  if (Object.hasOwn(value.presets, preferredKey)) $('preset-select').value = String(preferredKey);
  editPreset($('preset-select').value);
}
async function presets() {
  if (presetsBusy) return;
  presetBusy(true);
  try {showPresets(await api('presets'));} finally {presetBusy(false);}
}
async function route() {
  if (!session) return;
  const hash = location.hash.slice(1) || 'accounts', match = /^account-(\d+)$/.exec(hash);
  let view = match ? 'accounts' : hash;
  if (!['accounts', 'statistics', 'presets', 'activities', 'announcement', 'connection', 'administrators'].includes(view) || (view === 'administrators' && !session.is_superadmin)) view = 'accounts';
  for (const panel of document.querySelectorAll('[data-view]')) panel.hidden = panel.dataset.view !== view;
  for (const link of $('tabs').querySelectorAll('a')) {if (link.hash === '#' + view) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');}
  try {
    if (view === 'accounts') {
      if (match) {if (!$('account-list').children.length) await accounts(); await detail(Number(match[1]));}
      else {selected = null; detailRequest++; $('account-detail').hidden = true; await accounts();}
    } else {detailRequest++;}
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
    if (submit?.disabled) return;
    if (submit) submit.disabled = true; try {await action(event.currentTarget); notice('');} catch (error) {notice(error.message);} finally {if (submit) submit.disabled = false;}});
}
form('login-form', async f => {const value = await api('login', {username: f.elements.username.value, password: f.elements.password.value}); f.elements.password.value = ''; await start(value);});
form('accounts-form', async () => {await accounts(0);});
form('activity-form', async () => {activityDocument = await api('activities/active', {activity_id: $('activity-select').value || null, expected_version: activityDocument.version}); $('activity-status').textContent = '活动已保存。';});
form('announcement-form', async () => {const value = await api('announcement', {content: $('announcement-content').value, expected_version: announcementVersion}); announcementVersion = value.version; $('announcement-status').textContent = value.message;});
form('administrator-form', async f => {await api('administrators', {username: f.elements.username.value, password: f.elements.password.value}); f.reset(); await owners();});
form('preset-form', async () => {
  if (presetsBusy || !presetDocument) return;
  const entries = [...$('preset-entries').children].map(row => {
    const read = key => row.querySelector(`[data-field="${key}"]`).value.trim();
    return {pool_key: read('pool_key'), resource: read('resource'), pulls: Number(read('pulls'))};
  });
  const key = presetKey, body = {expected_version: presetDocument.version,
    preset: {name: $('preset-name').value.trim(), description: $('preset-description').value, entries}};
  presetBusy(true);
  try {
    const value = await api(key === null ? 'presets' : 'presets/' + encodeURIComponent(key), body, key === null ? 'POST' : 'PUT');
    showPresets(value, key === null ? String(value.created_id) : key);
    $('preset-status').textContent = `预设 #${presetKey} 已保存。`;
  } finally {presetBusy(false);}
});
$('logout').addEventListener('click', async () => {try {await api('logout', {}); loggedIn(null);} catch (error) {notice(error.message);}});
$('accounts-refresh').addEventListener('click', () => accounts().catch(e => notice(e.message)));
$('previous').addEventListener('click', () => {if (!accountsLoading && offset > 0) accounts(Math.max(0, offset - accountPageSize)).catch(e => notice(e.message));});
$('next').addEventListener('click', () => {if (!accountsLoading && offset + accountPageSize < total) accounts(offset + accountPageSize).catch(e => notice(e.message));});
$('statistics-preset').addEventListener('change', statistics); $('scope').addEventListener('change', statistics); $('statistics-owner').addEventListener('change', statistics); $('refresh').addEventListener('click', statistics);
$('activity-select').addEventListener('change', activityDetails); $('preset-select').addEventListener('change', () => editPreset($('preset-select').value));
$('preset-new').addEventListener('click', () => {if (!presetsBusy) {editPreset(''); $('preset-name').focus();}});
$('preset-add-entry').addEventListener('click', () => {if (!presetsBusy && $('preset-entries').children.length < 64) entryFields();});
$('preset-delete').addEventListener('click', async () => {
  if (presetsBusy || presetKey === null || !window.confirm(`删除共享预设 #${presetKey}？已开始的运行不受影响。`)) return;
  presetBusy(true);
  try {
    const value = await api('presets/' + encodeURIComponent(presetKey) + '?expected_version=' + presetDocument.version, undefined, 'DELETE');
    showPresets(value); $('preset-status').textContent = '预设已删除。'; notice('');
  } catch (error) {notice(error.message);} finally {presetBusy(false);}
});
function showCredential(value) {credential = value; $('credential').textContent = value.token; $('credential').hidden = false; $('credential-copy').hidden = false;}
$('credential-show').addEventListener('click', async () => {try {showCredential(await api('credential'));} catch (error) {notice(error.message);}});
$('credential-copy').addEventListener('click', async () => {try {await navigator.clipboard.writeText(credential.token); $('credential-status').textContent = '凭据已复制。';} catch {$('credential-status').textContent = '复制失败，请手动选择凭据。';}});
$('credential-reset').addEventListener('click', async () => {if (!window.confirm('重置后，本人的全部旧接入和连接都会失效。')) return; try {showCredential(await api('credential/reset', {})); $('credential-status').textContent = '凭据已重置，请在各客户端更新。';} catch (error) {notice(error.message);}});
window.addEventListener('hashchange', route);
(async () => {try {await start(await api('session'));} catch {loggedIn(null);}})();
