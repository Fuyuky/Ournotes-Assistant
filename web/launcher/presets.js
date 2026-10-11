/* Shared cloud preset editor. Saved account plans remain read-only. */
(() => {
  const page = {data:null, name:null, draft:null, editing:false, creating:false, deleting:false, loading:false, saving:false, sequence:0};
  const resourceName = r => r === 'stars' ? '免费星钻' : '招募券';
  const batches = pulls => {
    const n=Math.max(0,Math.min(1000,Math.trunc(Number(pulls) || 0)));
    return [...Array(Math.floor(n/10)).fill(10),...Array(n%10).fill(1)];
  };
  const pool = key => page.data?.pools.find(p => p.key === key || `bdon-gacha-${p.gacha_id}` === key);
  const selected = () => page.data?.presets.find(p => p.name === page.name);
  const cardLabel = key => {
    const c = page.data?.cards[key];
    return c ? `${c.kind === 'member' ? '角色卡' : '支援卡'} ${c.name}${c.title ? '【'+c.title+'】' : ''} (${key})` : key;
  };
  function totals(entries) {
    const value = {pulls:0, transactions:0, stars:0, tickets:0, cost:0};
    for (const e of entries) {
      const n = Number(e.pulls) || 0;
      value.pulls += n;
      value.transactions += batches(n).length;
      value[e.resource === 'stars' ? 'stars' : 'tickets'] += n;
      if (e.resource === 'stars') for (const size of batches(n)) {
        const price = pool(e.pool_key)?.star_prices?.[size];
        value.cost = value.cost === null || price === undefined ? null : value.cost + price;
      }
    }
    return value;
  }
  function splitText(n) {
    const tens = Math.floor(n / 10), ones = n % 10;
    return [tens ? `${tens} 次十连` : '', ones ? `${ones} 次单抽` : ''].filter(Boolean).join(' + ') || '请填写抽数';
  }
  function entryNote(e) {
    const p = pool(e.pool_key);
    return `${splitText(Number(e.pulls) || 0)} · ${e.resource === 'ticket' ? (p?.ticket_name || '券种待核验') : '仅使用免费星钻'}${e.until_owned ? ' · 集齐目标后转池' : ''}`;
  }
  function updateSummary() {
    if (!page.draft) return;
    const t = totals(page.draft.entries);
    $('preset-summary').textContent = `实际 ${t.pulls} 抽 · ${t.transactions} 笔交易 · 星钻招募 ${t.stars} 抽 / 券招募 ${t.tickets} 抽 · 免费星钻需求 ${t.cost ?? '待核验'}`;
    $('preset-save').disabled = page.saving || page.loading || !page.editing;
    $('preset-add').disabled = page.draft.entries.length >= 64;
    $('preset-add').hidden = !page.editing;
  }
  function poolSelect(value, starsOnly=false) {
    const select = el('select');
    for (const p of page.data.pools) {
      if ((!p.supported || !p.resources.length || starsOnly && !p.resources.includes('stars')) && p.key !== value) continue;
      const option = el('option','',`${p.name} · #${p.gacha_id}${p.supported ? '' : '（暂不支持）'}`);
      option.value = p.key; select.append(option);
    }
    if (![...select.options].some(o => o.value === value)) {
      const option = el('option','',pool(value)?.name || `未知卡池：${value}`);
      option.value = value; select.append(option);
    }
    select.value = value; return select;
  }
  function field(label, input) {
    const node = el('label','field',label); node.append(input); return node;
  }
  function policyView(e, row) {
    const p = pool(e.pool_key);
    if (!e.until_owned && (!page.editing || e.resource !== 'stars' || !p?.pickup_card_keys?.length)) return;
    const label = el('label','preset-policy-toggle');
    const enabled = el('input'); enabled.type='checkbox'; enabled.checked=!!e.until_owned;
    enabled.disabled = e.resource !== 'stars' || !p?.pickup_card_keys?.length;
    label.append(enabled,el('span','',`集齐本池 ${p?.pickup_card_keys?.length || 0} 张 UP 后，将剩余星钻招募转池`));
    enabled.addEventListener('change',()=>{
      if (enabled.checked) {
        e.until_owned = [...p.pickup_card_keys];
        e.fallback_pool_key = page.data.pools.find(v=>v.key==='our-notes-standard' && v.key!==p.key && v.supported && v.resources.includes('stars'))?.key
          || page.data.pools.find(v=>v.key!==p.key && v.supported && v.resources.includes('stars'))?.key;
      } else {delete e.until_owned; delete e.fallback_pool_key;}
      renderEntries(); updateSummary();
    });
    row.append(label);
    if (e.until_owned) {
      const targets = el('div','preset-targets');
      for (const key of e.until_owned) targets.append(el('span','',cardLabel(key)));
      const fallback = poolSelect(e.fallback_pool_key,true);
      fallback.addEventListener('change',()=>{e.fallback_pool_key=fallback.value;});
      row.append(targets,field('转入卡池',fallback),el('p','help','仅依据本账号已完整识别的结果判断集齐；先消耗剩余招募券，再执行转池星钻招募。'));
    }
  }
  function renderEntries() {
    const rows = page.draft.entries.map((e,i)=>{
      const row = el('section','preset-entry');
      const head = el('div','preset-entry-head'); head.append(el('b','',`项目 ${i+1}`));
      const actions = el('div','control-bar');
      actions.hidden=!page.editing;
      for (const [text,offset] of [['上移',-1],['下移',1],['移除',0]]) {
        const button = el('button','button plain small',text); button.type='button';
        button.disabled = !offset ? page.draft.entries.length===1 : i+offset<0 || i+offset>=page.draft.entries.length;
        button.setAttribute('aria-label',`${text}项目 ${i+1}`);
        button.addEventListener('click',()=>{
          if (!offset) page.draft.entries.splice(i,1);
          else [page.draft.entries[i],page.draft.entries[i+offset]]=[page.draft.entries[i+offset],page.draft.entries[i]];
          renderEntries();updateSummary();
        });actions.append(button);
      }
      head.append(actions); row.append(head);
      const grid = el('div','preset-entry-grid');
      const pools = poolSelect(e.pool_key);
      pools.addEventListener('change',()=>{
        e.pool_key=pools.value;
        if (!pool(e.pool_key)?.resources.includes(e.resource)) e.resource=pool(e.pool_key)?.resources[0] || 'stars';
        delete e.until_owned;delete e.fallback_pool_key;renderEntries();updateSummary();
      });
      const resource = el('select');
      for (const key of new Set([...(pool(e.pool_key)?.resources || []),e.resource])) {
        const option=el('option','',resourceName(key));option.value=key;resource.append(option);
      }
      resource.value=e.resource;
      resource.addEventListener('change',()=>{e.resource=resource.value;delete e.until_owned;delete e.fallback_pool_key;renderEntries();updateSummary();});
      const pulls=el('input');pulls.type='number';pulls.min='1';pulls.max='1000';pulls.step='1';pulls.required=true;pulls.value=e.pulls;
      const note=el('p','help',entryNote(e));
      pulls.addEventListener('input',()=>{e.pulls=Number(pulls.value);note.textContent=entryNote(e);updateSummary();});
      grid.append(field('卡池',pools),field('资源',resource),field('抽数',pulls));
      row.append(grid,note);policyView(e,row);return row;
    });
    $('preset-entries').replaceChildren(...rows);
  }
  function renderList() {
    $('preset-create').disabled=!page.data.editable || page.editing || page.loading || page.saving || !page.data.pools.some(p=>p.supported && p.resources.length);
    $('preset-list').replaceChildren(...page.data.presets.map(p=>{
      const button=el('button',`preset-choice${p.name===page.name?' active':''}`);
      button.type='button';button.disabled=page.editing || page.loading || page.saving;
      button.append(el('b','',p.name),el('small','',`实际 ${p.total_pulls} 抽 · ${p.transactions} 笔交易${p.validation_errors.length?' · 需要检查':''}`));
      button.setAttribute('aria-current',p.name===page.name?'true':'false');
      button.addEventListener('click',()=>{page.name=p.name;renderPreset();});return button;
    }));
  }
  function renderPreset() {
    const preset=selected();
    page.draft=preset ? structuredClone(preset) : null;page.editing=false;page.creating=false;page.deleting=false;
    $('preset-new-name-field').hidden=true;$('preset-new-name').disabled=true;$('preset-new-name').required=false;
    $('preset-name').textContent=preset?.name || '尚无共享预设';
    $('preset-description').value=preset?.description || '';
    const notes=[...(preset?.validation_errors || [])];
    const named=preset?.name.match(/(\d+)\s*抽/);
    if (named && Number(named[1])!==preset.total_pulls) notes.push(`名称中的 ${named[1]} 抽与实际总数 ${preset.total_pulls} 抽不同，请以项目明细为准。`);
    $('preset-validation').textContent=notes.join(' ');$('preset-validation').hidden=!notes.length;
    $('preset-fields').disabled=true;$('preset-edit-actions').hidden=true;
    $('preset-edit').disabled=!preset || !page.data.editable || page.loading || page.saving;
    $('preset-delete').disabled=!preset || !page.data.editable || page.loading || page.saving;
    renderList();
    if (preset) {renderEntries();updateSummary();}
    else {$('preset-entries').replaceChildren();$('preset-summary').textContent='可增加预设供后续批次选择。';$('preset-add').hidden=true;$('preset-save').disabled=true;}
  }
  function renderSaved() {
    const saved=page.data.saved;
    $('preset-plans-note').textContent=saved.error || (saved.batch_id?`批次 ${saved.batch_id.slice(0,8)} · ${statuses[saved.batch_status] || saved.batch_status} · 下方取自已保存账号，修改预设不会重写这些计划。`:'尚无已保存批次。');
    $('preset-saved-plans').replaceChildren(...saved.plans.map(item=>{
      const details=el('details','preset-saved');
      const total=item.plan.reduce((sum,e)=>sum+e.pulls,0);
      details.append(el('summary','',`MuMu ${item.instance} · #${item.index} · ${item.preset} · 实际 ${total} 抽${item.matches_preset===true?' · 与当前预设一致':item.matches_preset===false?' · 与当前配置不同，保留原计划':' · 当前预设不存在'}`));
      const table=el('table','report-table'),head=el('tr');
      for(const text of ['交易','卡池','资源','抽数','状态']) head.append(el('th','',text));table.append(head);
      item.plan.forEach((e,i)=>{
        const row=el('tr'),status=item.draws.find(d=>d.ordinal===i)?.status;
        for(const text of [i+1,pool(e.pool_key)?.name || e.pool_key,resourceName(e.resource),e.pulls,{complete:'已识别',submitted:'已提交',captured:'待识别',needs_review:'待核查',planned:'已规划'}[status] || '未执行']) row.append(el('td','',String(text)));
        table.append(row);
        if(e.until_owned){const policy=el('tr');const td=el('td','',`集齐 ${e.until_owned.map(cardLabel).join('、')} 后转入 ${pool(e.fallback_pool_key)?.name || e.fallback_pool_key}`);td.colSpan=5;policy.append(td);table.append(policy);}
      });const scroll=el('div','table-scroll');scroll.append(table);details.append(scroll);return details;
    }));
    if (!saved.plans.length) $('preset-saved-plans').append(el('p','help','本批没有可展示的活动账号计划。'));
  }
  async function load() {
    if (page.editing || page.saving || page.loading) return;
    page.loading=true;const sequence=++page.sequence;
    $('preset-create').disabled=true;
    $('presets-refresh').disabled=true;$('presets-refresh').classList.add('is-loading');
    $('presets-notice').hidden=false;$('presets-notice').textContent='正在读取预设与已保存计划…';
    try {
      const data=await api('presets');if(sequence!==page.sequence)return;
      page.data=data;
      if (!selected()) page.name=data.presets[0]?.name;
      page.loading=false;
      $('presets-notice').textContent=data.blocked_reason || `共享版本 ${data.version}；保存用于后续分配，活动账号继续原计划。资源不足时跳过该项，不购买资源。`;
      renderPreset();renderSaved();
    } catch(error) {
      $('presets-notice').textContent=error.message;
      $('preset-edit').disabled=true;
    } finally {
      page.loading=false;$('presets-refresh').disabled=false;$('presets-refresh').classList.remove('is-loading');
    }
  }
  $('presets-refresh').addEventListener('click',load);
  $('preset-create').addEventListener('click',()=>{
    if(!page.data?.editable || page.editing || page.loading || page.saving)return;
    const p=page.data.pools.find(p=>p.supported && p.resources.length);if(!p)return;
    page.creating=true;page.deleting=false;page.editing=true;
    page.draft={name:'',description:'',entries:[{pool_key:p.key,resource:p.resources[0],pulls:1}]};
    $('preset-name').textContent='增加预设';$('preset-description').value='';
    $('preset-validation').hidden=true;$('preset-fields').disabled=false;
    $('preset-new-name-field').hidden=false;$('preset-new-name').disabled=false;
    $('preset-new-name').required=true;$('preset-new-name').value='';$('preset-new-name').setCustomValidity('');
    $('preset-edit-actions').hidden=false;$('preset-edit').disabled=true;$('preset-delete').disabled=true;$('presets-refresh').disabled=true;
    renderList();renderEntries();updateSummary();$('preset-new-name').focus();
  });
  $('preset-new-name').addEventListener('input',()=>{
    page.draft.name=$('preset-new-name').value.trim();$('preset-new-name').setCustomValidity('');
  });
  $('preset-edit').addEventListener('click',()=>{
    if(!page.data?.editable || page.loading || page.saving)return;
    page.deleting=false;page.editing=true;$('preset-fields').disabled=false;$('preset-edit-actions').hidden=false;$('preset-edit').disabled=true;$('preset-delete').disabled=true;
    $('presets-refresh').disabled=true;renderList();renderEntries();updateSummary();$('preset-description').focus();
  });
  $('preset-description').addEventListener('input',()=>{page.draft.description=$('preset-description').value;});
  $('preset-cancel').addEventListener('click',()=>{renderPreset();$('presets-refresh').disabled=false;});
  $('preset-add').addEventListener('click',()=>{
    const p=page.data.pools.find(p=>p.supported && p.resources.length);
    page.draft.entries.push({pool_key:p.key,resource:p.resources[0],pulls:10});renderEntries();updateSummary();
  });
  $('preset-form').addEventListener('submit',event=>{
    event.preventDefault();page.deleting=false;
    if(page.creating){
      const name=page.draft.name;
      $('preset-new-name').setCustomValidity(!name?'请输入预设名称。':page.data.presets.some(p=>p.name===name)?'预设名称已存在，请使用其他名称。':'');
    }
    if(!page.editing || page.saving || !$('preset-form').reportValidity())return;
    const old=selected(),t=totals(page.draft.entries);
    $('preset-save-title').textContent='确认保存预设';$('preset-save-back').textContent='返回编辑';
    $('preset-save-confirm').textContent='确认保存';
    $('preset-save-summary').textContent=page.creating?`增加「${page.draft.name}」：${t.pulls} 抽，${t.transactions} 笔交易。星钻招募 ${t.stars} 抽，券招募 ${t.tickets} 抽。保存后可在新批次中选择。`:`${page.name}：${old.total_pulls} → ${t.pulls} 抽，${old.transactions} → ${t.transactions} 笔交易。星钻招募 ${t.stars} 抽，券招募 ${t.tickets} 抽。保存将用于新批次，已分配账号和交易保持原样。`;
    $('preset-save-dialog').showModal();
  });
  $('preset-save-back').addEventListener('click',()=>{if(!page.saving)$('preset-save-dialog').close();});
  $('preset-delete').addEventListener('click',()=>{
    if (!selected() || !page.data?.editable || page.editing || page.loading || page.saving) return;
    page.deleting=true;
    $('preset-save-title').textContent='确认删除预设';$('preset-save-back').textContent='取消';
    $('preset-save-summary').textContent=`从共享列表删除「${page.name}」。后续批次将不能选择此预设；已分配账号继续保存的原计划。`;
    $('preset-save-confirm').textContent='确认删除';
    $('preset-save-dialog').showModal();
  });
  $('preset-save-dialog').addEventListener('cancel',event=>{if(page.saving)event.preventDefault();});
  $('preset-save-confirm').addEventListener('click',async()=>{
    if(page.saving)return;page.saving=true;
    const confirm=$('preset-save-confirm');confirm.disabled=true;confirm.classList.add('is-loading');confirm.textContent='正在校验并保存…';
    $('preset-save-back').disabled=true;$('preset-fields').disabled=true;$('preset-cancel').disabled=true;
    const value={operation:page.deleting?'delete':page.creating?'create':'update',key:selected()?.key,name:page.creating?page.draft.name:page.name,description:page.draft.description || '',expected_version:page.data.version,
      entries:page.draft.entries.map(e=>Object.fromEntries(['pool_key','resource','pulls','until_owned','fallback_pool_key'].filter(k=>k in e).map(k=>[k,e[k]])))};
    try {
      const data=await api('presets',value);page.data=data;page.name=data.presets.some(p=>p.name===value.name)?value.name:data.presets[0]?.name;page.saving=false;page.deleting=false;
      renderPreset();renderSaved();$('presets-refresh').disabled=false;
      $('presets-notice').textContent=data.message;toast(data.message);$('preset-save-dialog').close();
      await refresh();runSummary();
    } catch(error) {
      $('preset-save-dialog').close();$('presets-notice').textContent=`保存未确认：${error.message} 草稿已保留；如提示版本变化，请取消修改后刷新核对。`;
      $('preset-fields').disabled=false;toast(error.message);
    } finally {
      page.saving=false;confirm.disabled=false;confirm.classList.remove('is-loading');confirm.textContent='确认保存';
      $('preset-save-back').disabled=false;$('preset-cancel').disabled=false;updateSummary();
    }
  });
  window.addEventListener('hashchange',()=>{if(location.hash==='#presets')load();});
  let cloudRevision;
  window.consoleState.subscribe(data=>{
    if (cloudRevision !== undefined && data.preset_version !== cloudRevision && location.hash==='#presets') load();
    cloudRevision=data.preset_version;
  });
  if(location.hash==='#presets')load();
})();
