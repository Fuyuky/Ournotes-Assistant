"use strict";
function createAccountQuery({api, getIndex, input, button, result, summary, preset, cards, el}) {
  let serial = 0, loaded = null;
  const label = button.textContent;
  function busy(value) {
    button.disabled = value;
    button.textContent = value ? '正在查询…' : label;
    button.setAttribute('aria-busy', String(value));
    button.classList.toggle('is-loading', value);
    result.setAttribute('aria-busy', String(value));
    cards.setAttribute('aria-busy', String(value));
  }
  function clearCards() {
    cards.hidden = true;
    cards.replaceChildren();
  }
  function renderCards(items) {
    if (!Array.isArray(items)) throw new Error('控制台尚未加载卡牌信息，请退出并重新打开控制台后查询。');
    const groups = [];
    for (const [kind, label] of [['member', '角色卡'], ['support', '支援卡']]) {
      const owned = items.filter(card => card.kind === kind);
      if (!owned.length) continue;
      const group = el('section', 'account-card-group');
      group.append(el('h3', '', `SSR ${label} · ${owned.reduce((n, card) => n + card.count, 0)} 张`));
      const grid = el('div', `account-card-grid ${kind}`);
      for (const card of owned) {
        const tile = el('figure', 'account-owned-card');
        const art = el('div', 'account-card-art');
        const image = el('img');
        image.src = card.image;
        image.alt = `${label} SSR【${card.title}】${card.name}`;
        image.loading = 'lazy';
        image.addEventListener('error', () => {image.alt = '卡图暂不可用';});
        art.append(image, el('span', 'account-card-count', `×${card.count}`));
        const caption = el('figcaption');
        caption.append(el('b', '', card.name), el('span', '', card.title));
        tile.append(art, caption);
        grid.append(tile);
      }
      group.append(grid);
      groups.push(group);
    }
    cards.replaceChildren(...(groups.length ? groups : [el('p', 'help', '此账号没有 SSR 卡牌。')]));
    cards.hidden = false;
  }
  function reset() {
    serial++; loaded = null; busy(false);
    result.hidden = true; summary.textContent = ''; preset.textContent = '';
    clearCards();
  }
  async function query() {
    let index;
    try {index = getIndex();} catch (error) {
      reset(); result.hidden = false; summary.textContent = error.message; return;
    }
    const current = ++serial;
    loaded = null; result.hidden = false;
    clearCards();
    summary.textContent = `正在查询账号 ${index}…`; preset.textContent = ''; busy(true);
    try {
      const value = await api(`accounts/${index}`);
      if (current !== serial) return;
      if (value.index !== index) throw new Error('返回的账号编号不一致，请重新查询。');
      renderCards(value.cards);
      loaded = index;
      summary.textContent = `账号 ${index} · ${({available:'未使用',booked:'已拍下',used:'已使用'})[value.usage_status] || (value.used ? '已使用' : '未使用')} · ${value.pull_count == null ? '抽数未记录' : value.pull_count + ' 抽'} · ${value.ssr_total} SSR`;
      if (value.usage_pending) summary.textContent += ' · 状态待云端确认';
      const saved = value.historical_preset;
      preset.textContent = saved?.status === 'recorded'
        ? `使用预设：${saved.name}${saved.planned_pulls == null ? '' : ` · 保存计划 ${saved.planned_pulls} 抽`}`
        : saved?.status === 'unavailable' ? '预设记录暂时不可读，请稍后重试。'
        : '云端成果未记录历史预设。';
      if (value.owner_username) preset.textContent += ` · 管理员 ${value.owner_username}`;
      if (value.activity?.name) preset.textContent += ` · 活动 ${value.activity.name}`;
    } catch (error) {
      if (current !== serial) return;
      summary.textContent = error.message === 'Not Found'
        ? '控制台尚未加载账号查询接口；本轮运行结束后退出并重新打开控制台。' : error.message;
      preset.textContent = '';
      clearCards();
    } finally {if (current === serial) busy(false);}
  }
  input.addEventListener('input', reset);
  input.addEventListener('keydown', event => {if (event.key === 'Enter') {event.preventDefault(); query();}});
  button.addEventListener('click', query);
  return {query, reset, ensure() {if (loaded !== getIndex()) return query();}};
}
