"use strict";
(() => {
  const form = document.getElementById('game-update-form');
  const button = document.getElementById('game-update-start');
  const progress = document.getElementById('game-update-progress');
  const cancel = document.getElementById('game-update-cancel');
  let requestId = null;
  const notificationToggle = document.getElementById('background-notifications');
  const notificationStatus = document.getElementById('notification-status');
  const notificationTest = document.getElementById('notification-test');
  function renderNotifications(value) {
    notificationToggle.checked = value.enabled;
    notificationToggle.disabled = !value.available;
    notificationTest.disabled = !value.available || !value.enabled;
    notificationStatus.textContent = !value.available ? '后台通知在当前系统上不可用。' : value.error ||
      (value.enabled ? `后台通知已开启。${value.click_opens_console === false ? '当前系统通知不支持点击打开控制台。' : ''}` : '后台通知已关闭。');
  }
  notificationToggle.addEventListener('change', async () => {
    try { renderNotifications(await api('notifications/settings', {enabled: notificationToggle.checked})); }
    catch (error) { notificationStatus.textContent = error.message; }
  });
  notificationTest.addEventListener('click', async () => {
    try { notificationStatus.textContent = (await api('notifications/test', {})).message; }
    catch (error) { notificationStatus.textContent = error.message; }
  });
  function render(value) {
    button.disabled = value.status === 'running';
    cancel.hidden = value.status !== 'running';
    const amount = value.stage === 'downloading' && value.bytes
      ? ` · ${(value.bytes / 1048576).toFixed(1)} MiB${value.total_bytes ? ' / ' + (value.total_bytes / 1048576).toFixed(1) + ' MiB' : ''}` : '';
    const historical = !!value.historical;
    progress.textContent = historical ? '本次控制台尚未手动更新。' : (value.message || '尚未手动更新。') + amount;
    const history = document.getElementById('game-update-history');
    history.hidden = value.status === 'idle';
    if (!historical && value.status !== 'idle') history.open = true;
    document.getElementById('game-update-history-title').textContent = historical ? '上次更新结果（历史记录）' : '本次更新结果';
    document.getElementById('game-update-result-message').textContent = (value.message || '') + amount;
    document.getElementById('game-update-dismiss').hidden = value.status === 'running';
    document.getElementById('game-update-results').replaceChildren(...(value.results || []).map(result => {
      const item = document.createElement('li');
      item.textContent = `实例 ${result.instance}：${result.status === 'updated' ? '已更新' : '未覆盖'} · ${result.version}${result.message ? ' · ' + result.message : ''}`;
      return item;
    }));
  }
  form.addEventListener('input', () => { requestId = null; });
  cancel.addEventListener('click', async () => {
    try { render(await api('game-update/cancel', {})); }
    catch (error) { progress.textContent = error.message; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const instances = document.getElementById('game-update-instances').value.split(/[,，]/).map(s => s.trim());
    if (instances.some(s => !/^\d+$/.test(s) || Number(s) > 255)) {
      progress.textContent = '实例编号须为 0–255，以逗号分隔。'; return;
    }
    requestId ||= crypto.randomUUID().replaceAll('-', '');
    button.disabled = true;
    try {
      render(await api('game-update', {instances: instances.map(Number), request_id: requestId}));
    } catch (error) { progress.textContent = error.message; button.disabled = false; }
  });
  document.getElementById('game-update-dismiss').addEventListener('click', async () => {
    try {render(await api('game-update/dismiss', {})); await window.consoleState.refresh(true);}
    catch (error) {progress.textContent = error.message;}
  });
  window.consoleState.subscribe(data => {
    if (data.notifications) renderNotifications(data.notifications);
    if (data.game_update) {
      render(data.game_update);
      if (data.game_update.request_id === requestId && data.game_update.status !== 'running') requestId = null;
    }
  });
})();
