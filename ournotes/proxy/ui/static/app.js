"use strict";
(() => {
  const panel = document.getElementById('clash-panel');
  if (!panel) return;
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="launcher-token"]').content;
  let busy = false;
  const labels = {connected:'已连接',checking:'检查中',unavailable:'不可用',running:'任务执行中',paused:'批次已暂停',idle:'等待任务',complete:'本批已结束',ready:'额度可用',draining:'等待当前任务收尾',uncertain:'切换待确认',error:'切换失败',exhausted:'额度已满'};
  function badge(selector, key, label) {
    document.querySelectorAll(selector).forEach(n => {n.className=`clash-badge ${key}`; n.textContent=label || labels[key] || '状态未知';});
  }
  function render(data) {
    const c=data.connection, r=data.rotation, connected=c.state==='connected';
    badge('[data-clash="connection-badge"]',c.state);
    badge('[data-clash="rotation-badge"]',r.state);
    $('clash-check').disabled=busy || c.checking;
    $('clash-check').classList.toggle('is-loading',busy || c.checking);
    $('clash-check').textContent=c.checking?'正在检查连接…':'重新检查连接';
    $('clash-checked').textContent=c.checked_at?`最近检查 ${new Date(c.checked_at*1000).toLocaleTimeString('zh-CN',{hour12:false})}${c.checking?' · 正在更新':''}`:'首次连接检查中';
    $('clash-node').textContent=connected?c.node:'未确认';
    $('clash-next').textContent=connected?(r.next_node?`${r.next_node}（候选）`:'暂无其他有额度节点'):'连接确认后重新计算';
    $('clash-group').textContent=connected?`${c.group} · ${c.transport}`:(r.group || '未确认');
    $('clash-remaining-nodes').textContent=`${r.remaining_nodes ?? 0} 个`;
    const threshold=r.threshold || 150, used=r.used || 0, reserved=r.reserved || 0;
    $('clash-progress-title').textContent='24小时发信次数';
    $('clash-count').textContent=`${used} / ${threshold}`;
    $('clash-progress').max=threshold; $('clash-progress').value=Math.min(used,threshold);
    $('clash-progress').setAttribute('aria-label',`当前节点24小时已发信 ${used} 次，在途预留 ${reserved} 次`);
    $('clash-countdown').textContent=`在途预留 ${reserved} 次 · 可用额度 ${Math.max(threshold-used-reserved,0)} 次${r.batch_id?` · 本批已处理 ${r.processed} 个账号（${labels[r.batch_state] || '状态待确认'}）`:''}`;
    $('clash-expiry').textContent=r.earliest_available_at?`额度最早可恢复：${new Date(r.earliest_available_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}（北京时间）；不会自动恢复任务。`:'';
    const rows=document.createDocumentFragment();
    for(const node of r.nodes || []) {
      const row=document.createElement('tr');
      row.classList.toggle('is-current',!!node.current);
      for(const value of [`${node.name}${node.current?'（当前）':''}`,node.used,node.reserved,node.remaining]) {
        const cell=document.createElement('td'); cell.textContent=String(value); row.appendChild(cell);
      }
      rows.appendChild(row);
    }
    $('clash-nodes').replaceChildren(rows);
    const last=r.last_switch;
    $('clash-last').textContent=last?`累计切换 ${r.switch_count} 次 · 最近 ${new Date(last.at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})} 切至 ${last.node}`:'新版启用后尚未自动切换节点';
    let message=[c.message,r.message].filter(Boolean).join(' · ');
    if(r.pending_node) message+=` · 切换目标：${r.pending_node}`;
    $('clash-message').textContent=message;
    $('clash-message').classList.toggle('is-error',!connected && c.state!=='checking' || ['error','uncertain','unavailable'].includes(r.state));
    const summary=connected?`${c.node} · 24小时发信 ${used}/${threshold} · ${labels[r.state] || r.message}`:c.message;
    document.querySelectorAll('[data-clash="summary"]').forEach(n=>n.textContent=summary);
  }
  async function refresh(force=false) {
    if(busy) return;
    busy=true;
    $('clash-check').disabled=true; $('clash-check').classList.add('is-loading');
    if(force) $('clash-check').textContent='正在检查连接…';
    try {
      const response=await fetch(`/api/launcher/clash/${force?'check':'status'}`,{cache:'no-store',signal:AbortSignal.timeout(8000),...(force?{method:'POST',headers:{'X-Launcher-Token':token}}:{})});
      if(!response.ok) throw new Error('无法读取 Clash 状态，请确认控制台仍在运行。');
      const data=await response.json();
      if(data.rotation?.protocol!==2) throw new Error('节点用量尚未就绪，请重启控制台加载新版；换绑任务的临时密码请先保存。');
      busy=false; render(data);
    } catch(error) {
      badge('[data-clash="connection-badge"]','unavailable','状态不可读');
      badge('[data-clash="rotation-badge"]','unavailable','状态未更新');
      $('clash-message').textContent=error.name==='TimeoutError'?'状态读取超时，请重新检查连接。':error.message;
      $('clash-message').classList.add('is-error');
      document.querySelectorAll('[data-clash="summary"]').forEach(n=>n.textContent='无法更新连接状态，请重新检查。');
      $('clash-check').disabled=false; $('clash-check').classList.remove('is-loading');
      $('clash-check').textContent='重新检查连接';
    } finally {busy=false; window.consoleState.refresh(true).catch(()=>{});}
  }
  $('clash-check').addEventListener('click',()=>refresh(true));
  window.consoleState.subscribe(snapshot => {
    if (!busy && snapshot.clash) render(snapshot.clash);
  });
})();
