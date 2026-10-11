"use strict";
const $ = id => document.getElementById(id);
const state = {cards: [], bands:{}, labels: new Map(), selected: new Set(), page: 1, pages: 1, order: "price_desc", size: 10, serial: 0, rendered: "", controller: null};
const kindName = kind => kind === "member" ? "角色卡" : "支援卡";
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}
function artwork(card, lazy = true) {
  const img = node("img");
  img.src = card.image;
  img.alt = `${kindName(card.kind)} SSR【${card.title}】${card.name}`;
  img.loading = lazy ? "lazy" : "eager";
  img.addEventListener("error", () => {img.classList.add("broken"); img.alt = "卡图暂不可用";});
  return img;
}
function renderCatalog() {
  const query = $("search").value.trim().toLocaleLowerCase();
  const fragment = document.createDocumentFragment();
  let shown = 0;
  for (const kind of ["member", "support"]) {
    const cards = state.cards.filter(c => c.kind === kind && `${c.name} ${c.title}`.toLocaleLowerCase().includes(query));
    if (!cards.length) continue;
    shown += cards.length;
    const section = node("div", `catalog-group ${kind}`);
    const heading = node("h3", "group-label", kindName(kind));
    heading.append(node("span", "tag", "SSR"), node("small", "", `${cards.length} 张可选`));
    const grid = node("div", "catalog-grid");
    for (const card of cards) {
      const button = node("button", "pick");
      button.type = "button";
      button.dataset.key = card.key;
      button.title = `SSR【${card.title}】${card.name}`;
      button.setAttribute("aria-label", `${kindName(kind)} SSR【${card.title}】${card.name}`);
      button.setAttribute("aria-pressed", String(state.selected.has(card.key)));
      const art = node("div", "art-wrap");
      art.append(artwork(card, false), node("span", "check", "✓"));
      button.append(art, node("span", "card-name", card.name), node("span", "card-title", card.title));
      button.addEventListener("click", () => toggle(card.key));
      grid.append(button);
    }
    section.append(heading, grid);
    fragment.append(section);
  }
  $("catalog").replaceChildren(fragment);
  $("catalog-empty").hidden = shown > 0;
}
function toggle(key) {
  state.selected.has(key) ? state.selected.delete(key) : state.selected.add(key);
  updateConditions();
  state.page = 1;
  loadAccounts();
}
function updateConditions() {
  for(const option of $('order').options)if(option.value.startsWith('selected_'))option.disabled=!state.selected.size;
  if(!state.selected.size && state.order.startsWith('selected_')){state.order='price_desc';$('order').value=state.order;}
  $('allow-missing').disabled=!state.selected.size;
  if(!state.selected.size)$('allow-missing').checked=false;
  $('selection-rule').textContent=$('allow-missing').checked?'至少拥有 1 种，已选择':'同时拥有';
  document.querySelectorAll(".pick").forEach(button => button.setAttribute("aria-pressed", String(state.selected.has(button.dataset.key))));
  $("selected-count").textContent = state.selected.size;
  $("clear").disabled = !state.selected.size;
  const chips = [...state.selected].map(key => {
    const card = state.labels.get(key);
    const chip = node("button", "chip", `${kindName(card.kind)} · ${card.name} ×`);
    chip.title = `取消 SSR【${card.title}】${card.name}`;
    chip.setAttribute("aria-label", chip.title);
    chip.addEventListener("click", () => toggle(key));
    return chip;
  });
  $("chips").replaceChildren(...chips);
}
function renderAccounts(data) {
  const publicCatalog = Boolean(data.meta.public_catalog || data.meta.public_account_limit);
  const signature = JSON.stringify([data.accounts, state.cards, state.bands, [...state.selected], data.meta.available, publicCatalog]);
  if (signature === state.rendered) return;
  state.rendered = signature;
  if (!data.accounts.length) {
    const empty = "没有匹配的账号。请检查 index 或卡牌条件；已使用或高稀有账号需要开启对应选项。";
    $("accounts").replaceChildren(node("div", "empty panel", data.meta.available ? empty : "账号目录暂时不可用，恢复后将自动更新。"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const account of data.accounts) {
    const article = node("article", "account");
    if(account.used)article.classList.add('is-used');
    if(!account.used && account.price?.status === 'pending'){
      article.classList.add('is-pending');
      article.append(node('span','pending-ribbon','高稀有账号'));
    }
    article.setAttribute("aria-label", `账号 ${account.index}`);
    const head = node("div", "account-head");
    const id = node("div", "account-id");
    id.append(node("small", "", "INDEX"), document.createTextNode(String(account.index).padStart(3, "0")));
    const total = node("div", "account-total");
    total.append(node("strong", "", account.ssr_total), document.createTextNode("SSR"));
    const badges=node('div','account-badges');
    if(account.used)article.append(node('span','usage-ribbon','已使用'));
    if(Number.isInteger(account.pull_count) && account.pull_count > 0)badges.append(node('span','pull-badge',`${account.pull_count} 抽`));
    for(const value of account.badges||[]){
      const [band,level]=value.split('-');const badge=node('span',`band-badge ${band} level-${level}`);
      if(band==='dream'||band==='millsage'){
        const activity=account.activity;
        if(!activity || !Number.isInteger(activity.up_total) || activity.up_total<=0)continue;
        badge.textContent=`${activity.label} UP ${activity.up_count}/${activity.up_total}`;
        badge.title=activity.up_count===activity.up_total?'本期 UP 已集齐':'活动卡池 UP 收集进度';
      }else if(level==='full'){
        const brand=state.bands[band],img=node('img');img.src=brand.logo;img.alt=brand.name;
        badge.append(img,node('span','','全队 SSR'));badge.title=`全 ${brand.name} SSR · 5 位不同角色`;
        badge.setAttribute('aria-label',badge.title);
      }else{badge.textContent=`${level} ${band==='mygo'?'MyGO':'Mujica'}`;badge.title=`${level} 位不同 ${band==='mygo'?'MyGO!!!!!':'Ave Mujica'} SSR 角色`;}
      badges.append(badge);
    }
    if(account.activity && account.activity.up_total>0 &&
       !(account.badges||[]).some(b=>b.startsWith('dream-')||b.startsWith('millsage-'))){
      badges.append(node('span','band-badge',`${account.activity.label} UP ${account.activity.up_count}/${account.activity.up_total}`));
    }
    const price=node('div','account-price');
    if(account.price?.status === 'priced'){
      price.append(node('small','','价格'),node('strong','',`~￥${(account.price.amount_cents/100).toLocaleString('zh-CN')}`));
    }else{
      price.append(node('small','','价格'),node('strong','','单独报价'));
      if(account.price?.reason === 'unverified_pulls')price.append(node('span','','抽数待核实'));
    }
    head.append(id,badges,total,price);
    const strip = node("div", "card-strip");
    strip.tabIndex = 0;
    strip.setAttribute("aria-label", `账号 ${account.index} 的 SSR 卡牌，可横向滚动`);
    for (const owned of account.cards) {
      const card = state.cards.find(c => c.key === owned.key);
      const tile = node("div", `owned-card${state.selected.has(owned.key) ? " required" : ""}`);
      tile.title = `${kindName(card.kind)} SSR【${card.title}】${card.name} ×${owned.count}`;
      const art = node("div", "owned-art");
      art.append(artwork(card), node("span", "quantity", `×${owned.count}`));
      const name = node("div", "owned-name", card.name);
      name.append(node("span", "kind-label", kindName(card.kind)));
      tile.append(art, name, node("div", "owned-title", card.title));
      strip.append(tile);
    }
    if (!account.cards.length) strip.append(node("span", "", "此账号没有 SSR 卡牌。"));
    article.append(head);
    if(account.local_preset){
      const p=account.local_preset;
      const text=p.status==='recorded'?`使用预设：${p.name}${p.planned_pulls==null?'':` · 保存计划 ${p.planned_pulls} 抽`}`:p.status==='unavailable'?'预设记录暂时不可读':'未记录预设';
      article.append(node('p','account-preset',text));
    }
    article.append(strip); fragment.append(article);
  }
  $("accounts").replaceChildren(fragment);
}
async function loadAccounts(poll = false, refresh = false) {
  if (poll && state.controller) return;
  if (state.controller) state.controller.abort();
  const controller = new AbortController();
  state.controller = controller;
  const serial = ++state.serial;
  const index=$('index-search').value.trim();
  if(index&&(!/^\d+$/.test(index)||!Number.isSafeInteger(Number(index)))){
    $('notice').hidden=false;$('notice').textContent='请输入完整的非负整数 index。';state.controller=null;
    $('accounts').replaceChildren(node('div','empty panel','index 无效，请检查输入。'));state.rendered='';
    $('accounts').setAttribute('aria-busy','false');$('match-count').textContent='—';$('range').textContent='请检查 index';
    $('prev').disabled=true;$('next').disabled=true;$('page-label').textContent='—';return;
  }
  const options = {cards: [...state.selected].join(","), order: state.order, page: state.page, page_size: state.size,index,include_used:$('include-used').checked,include_pending:$('include-pending').checked,allow_missing:$('allow-missing').checked,pull_count:$('pull-count').value};
  $("accounts").setAttribute("aria-busy", "true");
  try {
    const data = await accountSource.query(options, controller.signal, poll || refresh);
    if (serial !== state.serial) return;
    showAnnouncement(data.announcement);
    const publicCatalog = Boolean(data.meta.public_catalog || data.meta.public_account_limit);
    document.body.classList.toggle('public-catalog', publicCatalog);

    state.bands=data.bands||{};
    if (JSON.stringify(state.cards) !== JSON.stringify(data.catalog)) {
      state.cards = data.catalog;
      state.cards.forEach(card => state.labels.set(card.key, card));
      renderCatalog(); updateConditions();
    }
    state.page = data.page; state.pages = data.pages;
    $("valid-count").textContent = data.meta.valid_accounts;
    $("used-count").textContent = data.meta.used_accounts||0;
    $("loaded-at").textContent = data.meta.loaded_at ? `${accountSource.mode === "static" ? "数据更新" : "最后读取"} ${new Date(data.meta.loaded_at).toLocaleString("zh-CN", {hour12: false})}` : "尚未读取数据";
    $("match-count").textContent = data.total;
    const retired = [...state.selected].some(key => !state.cards.some(card => card.key === key));
    $("notice").hidden = data.meta.available && !retired;
    $("notice").textContent = retired ? "所选卡牌已从当前目录移除，请取消对应条件后继续筛选。" : "账号目录暂不可用，当前结果已清空。服务恢复后将自动读取。";
    renderAccounts(data);
    $("page-label").textContent = `${data.page} / ${data.pages}`;
    $("range").textContent = data.total ? `显示 ${(data.page-1)*state.size+1}–${Math.min(data.page*state.size, data.total)} / 共 ${data.total} 个账号` : "共 0 个账号";
    $("prev").disabled = data.page <= 1;
    $("next").disabled = data.page >= data.pages;
  } catch (error) {
    if (error.name === "AbortError" || serial !== state.serial) return;
    $("notice").hidden = false;
    if(error.message === 'outdated_backend'){
      $("notice").textContent = '当前筛选服务尚未载入价格版本，需要在运行任务结束后重启控制台。';
      if(['127.0.0.1','localhost'].includes(location.hostname) && location.port === '8766'){
        const link=node('a','',' 打开新版本地筛选页');link.href='http://127.0.0.1:8765/';link.target='_blank';link.rel='noopener';$('notice').append(link);
      }
      $('accounts').replaceChildren(node('div','empty panel','筛选服务等待更新，请使用新版本地筛选页。'));state.rendered='';
    }else{
      $("notice").textContent = "暂时无法更新数据，请检查服务是否运行。下方保留上次成功读取的结果。";
    }
  } finally {
    if (serial === state.serial) {state.controller = null; $("accounts").setAttribute("aria-busy", "false");}
  }
}
$("search").addEventListener("input", renderCatalog);
let indexTimer;
$('index-search').addEventListener('input',()=>{clearTimeout(indexTimer);state.page=1;indexTimer=setTimeout(()=>loadAccounts(),180);});
$('include-used').addEventListener('change',()=>{state.page=1;loadAccounts();});
$('include-pending').addEventListener('change',()=>{state.page=1;loadAccounts();});
$('pull-count').addEventListener('change',()=>{state.page=1;loadAccounts();});
$('allow-missing').addEventListener('change',()=>{updateConditions();state.page=1;loadAccounts();});
$("clear").addEventListener("click", () => {state.selected.clear(); updateConditions(); state.page = 1; loadAccounts();});
$("order").addEventListener("change", () => {state.order = $("order").value; updateConditions(); state.page = 1; loadAccounts();});
$("page-size").addEventListener("change", () => {state.size = Number($("page-size").value); state.page = 1; loadAccounts();});
$("refresh").addEventListener("click", () => loadAccounts(false, true));
for (const [id, delta] of [["prev", -1], ["next", 1]]) $(id).addEventListener("click", () => {state.page = Math.max(1, Math.min(state.pages, state.page + delta)); loadAccounts();});
loadAccounts(false, true);
setInterval(() => {if (!document.hidden) loadAccounts(true);}, 5000);
document.addEventListener("visibilitychange", () => {if (!document.hidden) loadAccounts(true);});

$('score-help').addEventListener('toggle',()=>{$('score-help-label').textContent=$('score-help').open?'折叠说明':'展开说明';});

let announcementVersion = null;
const acknowledgedAnnouncements = new Set();
function showAnnouncement(value) {
  if (!value?.content || !value.version) return;
  let acknowledged = acknowledgedAnnouncements.has(value.version);
  try { acknowledged ||= localStorage.getItem('ournotes-announcement') === value.version; } catch {}
  if (acknowledged) return;
  const dialog = $('site-announcement');
  announcementVersion = value.version;
  $('announcement-text').textContent = value.content;
  if (!dialog.open) dialog.showModal();
}
$('site-announcement').addEventListener('cancel',event=>event.preventDefault());
$('announcement-confirm').addEventListener('click',()=>{
  acknowledgedAnnouncements.add(announcementVersion);
  try {localStorage.setItem('ournotes-announcement',announcementVersion);} catch {}
  $('site-announcement').close();
});
