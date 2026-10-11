"use strict";
(() => {
  const listeners = new Set();
  let snapshot = null, socket = null, timer = null, pending = null, stopped = false;
  const token = document.querySelector('meta[name="launcher-token"]').content;
  function publish(value) {
    if (snapshot && value.stream_revision < snapshot.stream_revision) return;
    snapshot = value;
    for (const listener of listeners) {
      try {listener(value);} catch (error) {console.error('控制台面板更新失败', error);}
    }
  }
  function schedule(delay = 10000) {
    clearTimeout(timer);
    if (!stopped && (!socket || socket.readyState !== WebSocket.OPEN)) {
      timer = setTimeout(async () => {
        if (!document.hidden) await refresh().catch(() => {});
        connect(); schedule(snapshot?.running || snapshot?.email_rebind?.running ? 3000 : 10000);
      }, delay);
    }
  }
  async function refresh(force = false) {
    if (pending) return pending;
    pending = (async () => {
      const response = await fetch(`/api/launcher/${force ? 'refresh' : 'status'}`, {
        cache: 'no-store', ...(force ? {method: 'POST', headers: {'X-Launcher-Token': token}} : {})
      });
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail || '无法读取控制台状态。');
      publish(value); return value;
    })();
    try {return await pending;} finally {pending = null;}
  }
  function connect() {
    if (stopped || socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) return;
    try {
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/launcher/events`, ['ournotes', `session.${token}`]);
      socket.addEventListener('open', () => clearTimeout(timer));
      socket.addEventListener('message', event => {
        try {const value = JSON.parse(event.data); if (value.type === 'snapshot') publish(value.data);} catch {}
      });
      socket.addEventListener('close', () => {socket = null; schedule(3000);});
      socket.addEventListener('error', () => socket?.close());
    } catch {socket = null; schedule();}
  }
  window.consoleState = {
    subscribe(listener) {listeners.add(listener); if (snapshot) listener(snapshot); return () => listeners.delete(listener);},
    refresh,
    start() {connect(); refresh().catch(() => schedule(3000));},
    stop() {stopped = true; clearTimeout(timer); socket?.close();},
    get snapshot() {return snapshot;}
  };
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {connect(); if (!socket || socket.readyState !== WebSocket.OPEN) refresh().catch(() => {});}
  });
  window.addEventListener('pagehide', () => window.consoleState.stop());
})();
