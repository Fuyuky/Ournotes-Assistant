"use strict";
const accountApiBase = new URL("../api/", document.currentScript.src);
// Live backend adapter. Static exports substitute the offline adapter.
const accountSource = {
  mode: "live",
  catalog: null,
  async query(options, signal) {
    if (!this.catalog) {
      const response = await fetch(new URL("cards", accountApiBase), {signal, cache: "no-store"});
      if (!response.ok) throw new Error("catalog_failed");
      const data = await response.json();this.catalog = data.cards;this.bands = data.bands;
    }
    const response = await fetch(new URL(`accounts?${new URLSearchParams(options)}`, accountApiBase), {signal, cache: "no-store"});
    if (!response.ok) throw new Error(response.status === 422 && options.order.startsWith('price_') ? 'outdated_backend' : 'query_failed');
    const data = await response.json();
    if(data.accounts.some(account => !account.price))throw new Error('outdated_backend');
    return {...data, catalog: this.catalog, bands:this.bands};
  }
};
