const roots = ["taobao.com", "tmall.com"];

function allowedOrigin(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && !url.port &&
      roots.some((root) => url.hostname === root || url.hostname.endsWith(`.${root}`))
      ? url.origin : null;
  } catch { return null; }
}

export function normalizeCookies(items, now = Date.now() / 1000) {
  const cookies = new Map();
  let skipped = 0;
  for (const item of items) {
    const host = String(item.domain || "").replace(/^\./, "").toLowerCase();
    if (!roots.some((root) => host === root || host.endsWith(`.${root}`))) continue;
    if (!item.name || typeof item.value !== "string") {
      skipped++;
      continue;
    }
    if (!item.session && item.expirationDate != null &&
        (!Number.isFinite(item.expirationDate) || item.expirationDate <= now)) {
      skipped++;
      continue;
    }
    const cookie = {
      name: item.name,
      value: item.value,
      domain: item.hostOnly === true ? host : item.hostOnly === false ? `.${host}` : item.domain,
      path: item.path || "/",
      httpOnly: !!item.httpOnly,
      secure: !!item.secure,
      expires: item.session || item.expirationDate == null ? -1 : item.expirationDate,
    };
    const sameSite = { strict: "Strict", lax: "Lax", no_restriction: "None" }[item.sameSite];
    if (sameSite) cookie.sameSite = sameSite;
    if (item.partitionKey) {
      const site = allowedOrigin(item.partitionKey.topLevelSite);
      if (!site) { skipped++; continue; }
      cookie.partitionKey = site;
      if (typeof item.partitionKey.hasCrossSiteAncestor === "boolean")
        cookie._crHasCrossSiteAncestor = item.partitionKey.hasCrossSiteAncestor;
    }
    cookies.set(JSON.stringify([cookie.name, cookie.domain, cookie.path,
      cookie.partitionKey || null, cookie._crHasCrossSiteAncestor ?? null]), cookie);
  }
  return { state: { cookies: [...cookies.values()], origins: [] }, skipped };
}

export async function exportCookies(api) {
  // Read the current window's store, including an explicitly enabled incognito
  // window, without combining cookies belonging to different user sessions.
  const active = api.tabs ? (await api.tabs.query({ active: true, currentWindow: true }))[0] : null;
  const stores = api.cookies.getAllCookieStores ? await api.cookies.getAllCookieStores() : [];
  const store = stores.find((s) => s.tabIds.includes(active?.id));
  const storeFilter = store ? { storeId: store.id } : {};
  const queries = roots.flatMap((domain) => [
    { domain, ...storeFilter },
    ...roots.flatMap((root) => [false, true].map((hasCrossSiteAncestor) => ({
      domain, ...storeFilter,
      partitionKey: { topLevelSite: `https://${root}`, hasCrossSiteAncestor },
    }))),
  ]);
  const groups = await Promise.all(queries.map((q) => api.cookies.getAll(q)));
  const result = normalizeCookies(groups.flat());
  if (!result.state.cookies.length) {
    throw new Error("未找到可用的淘宝 Cookie。请先在同一浏览器、同一用户配置中登录淘宝，再重试。");
  }
  let storageFailures = 0;
  let storageFrames = 0;
  const origins = new Map();
  if (api.scripting && api.tabs) {
    const tabs = await api.tabs.query({ currentWindow: true,
      url: roots.map((domain) => `https://*.${domain}/*`) });
    tabs.sort((a, b) => Number(b.active) - Number(a.active) || (b.lastAccessed || 0) - (a.lastAccessed || 0));
    for (const tab of tabs) {
      const origin = allowedOrigin(tab.url);
      // Another tab with the same main origin can contain a different login
      // iframe, so deduplicate snapshots only after inspecting every tab.
      if (!origin || tab.incognito !== active?.incognito) continue;
      try {
        const frames = await api.scripting.executeScript({ target: { tabId: tab.id, allFrames: true },
          func: () => {
            // This function is serialized by Chrome: do not reference module
            // variables, and check scope before touching any page storage.
            const { protocol, hostname, port, origin } = location;
            if (protocol !== "https:" || port || !["taobao.com", "tmall.com"].some((root) =>
              hostname === root || hostname.endsWith(`.${root}`))) return null;
            try {
              return { origin, localStorage: Object.keys(localStorage).map((name) =>
                ({ name, value: localStorage.getItem(name) })) };
            } catch { return { origin, localStorage: null }; }
          } });
        let readMain = false;
        for (const frame of frames) {
          const state = frame.result;
          const frameOrigin = state && allowedOrigin(state.origin);
          if (!frameOrigin) continue;
          if (frameOrigin === origin) readMain = true;
          if (!Array.isArray(state.localStorage) || !state.localStorage.every((item) =>
            item && typeof item.name === "string" && typeof item.value === "string")) {
            storageFailures++;
            continue;
          }
          storageFrames++;
          if (!origins.has(frameOrigin)) origins.set(frameOrigin, {
            origin: frameOrigin, localStorage: state.localStorage,
          });
        }
        if (!readMain) storageFailures++;
      } catch { storageFailures++; }
    }
  }
  result.state.origins = [...origins.values()];
  const authSummary = Object.fromEntries(roots.map((root) => [root,
    ["unb", "_nk_", "lgc"].filter((name) => result.state.cookies.some((c) =>
      c.name === name && c.value && (c.domain === root || c.domain === `.${root}`))) ]));
  result.state.export_info = {
    tool: "guidajia-cookie-extension", version: "1.2.0", exported_at: new Date().toISOString(),
    cookie_count: result.state.cookies.length,
    partitioned_cookie_count: result.state.cookies.filter((c) => c.partitionKey).length,
    storage_origins: [...origins.keys()], storage_read_failures: storageFailures,
    storage_frames_read: storageFrames,
    missing_auth_storage_origins: ["https://login.taobao.com", "https://passport.taobao.com"]
      .filter((origin) => !origins.has(origin)),
    auth_markers: authSummary, indexed_db_exported: false,
  };
  return result;
}
