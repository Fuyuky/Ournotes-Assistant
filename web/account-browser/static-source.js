"use strict";
// No Python API: filter and paginate the public snapshot entirely in this browser.
function querySnapshot(snapshot, options) {
  const selected = new Set(options.cards.split(",").filter(Boolean));
  const known = new Set(snapshot.cards.map(card => card.key));
  // An expired card must not silently turn a previously selected AND query into all accounts.
  const invalid = [...selected].some(key => !known.has(key));
  const queryIndex = String(options.index ?? '').trim();
  if(queryIndex && (!/^\d+$/.test(queryIndex) || !Number.isSafeInteger(Number(queryIndex)))) throw new Error('invalid_index');
  const includeUsed = options.include_used === true || options.include_used === 'true';
  const includePending = options.include_pending === true || options.include_pending === 'true';
  const allowMissing = options.allow_missing === true || options.allow_missing === 'true';
  if(!['desc','asc','price_desc','price_asc','selected_desc','mygo_desc','mujica_desc','dream_desc','millsage_desc','dumb_desc'].includes(options.order))throw new Error('invalid_order');
  const selectedOrder = options.order === 'selected_desc';
  if(selectedOrder && !selected.size)throw new Error('select_cards_first');
  const pullCount = String(options.pull_count ?? '');
  if(!['','42','43','62'].includes(pullCount))throw new Error('invalid_pull_count');
  const matched = invalid ? [] : snapshot.accounts.filter(account => {
    if((account.used && !includeUsed && !queryIndex) || (queryIndex && account.index !== Number(queryIndex)))return false;
    if(!queryIndex && !account.used && account.price.status === 'pending' && !includePending)return false;
    if(pullCount && account.pull_count !== Number(pullCount))return false;
    const owned = new Set(account.cards.filter(card => card.count > 0).map(card => card.key));
    return !selected.size || (allowMissing ? [...selected].some(key => owned.has(key)) : [...selected].every(key => owned.has(key)));
  });
  const counts = new Map(matched.map(a => [a.index, a.cards.reduce((sum,c) => sum+(selected.has(c.key)?c.count:0),0)]));
  const kinds = new Map(matched.map(a => [a.index, new Set(a.cards.filter(c => selected.has(c.key) && c.count > 0).map(c => c.key)).size]));
  const priceCompare = (a,b,ascending=false) => {
    const pending = Number(b.price.status === 'pending')-Number(a.price.status === 'pending');
    return pending || (ascending ? 1 : -1)*((a.price.amount_cents??0)-(b.price.amount_cents??0)) || a.index-b.index;
  };
  matched.sort((a, b) => {
    if(selectedOrder)return counts.get(b.index)-counts.get(a.index) || kinds.get(b.index)-kinds.get(a.index) || priceCompare(a,b);
    if(options.order.startsWith('price_'))return priceCompare(a,b,options.order === 'price_asc');
    if(['mygo_desc','mujica_desc','dream_desc','millsage_desc','dumb_desc'].includes(options.order)){
      const metric = `${options.order.split('_')[0]}_characters`;
      return b[metric]-a[metric] || priceCompare(a,b);
    }
    return (options.order === 'asc' ? 1 : -1)*(a.ssr_total-b.ssr_total)||a.index-b.index;
  });
  const size = Number(options.page_size);
  const pages = Math.max(1, Math.ceil(matched.length / size));
  const page = Math.min(Math.max(1, Number(options.page)), pages);
  return {accounts: matched.slice((page-1)*size, page*size).map(a => selectedOrder ? {...a,selected_count:counts.get(a.index)} : a), total: matched.length, page, pages,
    page_size: size, meta: snapshot.meta, catalog: snapshot.cards, bands:snapshot.bands, announcement:snapshot.announcement};
}
const accountSource = {
  mode: "static",
  snapshot: null,
  async query(options, signal, refresh = false) {
    if (!this.snapshot || refresh) {
      // Retry once if a deployment switched between the pointer and snapshot requests.
      for (let attempt = 0; attempt < 2; attempt++) {
        const response = await fetch("data/current.json", {signal, cache: "no-store"});
        if (!response.ok) throw new Error("snapshot_unavailable");
        const pointer = await response.json();
        if (!/^[a-f0-9]{64}$/.test(pointer.revision)) throw new Error("invalid_revision");
        if (this.snapshot?.revision === pointer.revision) break;
        const data = await fetch(`data/snapshot.${pointer.revision}.json`, {signal, cache: "no-store"});
        if (!data.ok) {if (attempt === 0) continue; throw new Error("snapshot_unavailable");}
        const snapshot = await data.json();
        if (snapshot.schema_version !== 2 || snapshot.revision !== pointer.revision) throw new Error("invalid_snapshot");
        this.snapshot = snapshot;
        break;
      }
    }
    return querySnapshot(this.snapshot, options);
  }
};
