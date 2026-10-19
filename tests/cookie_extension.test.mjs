import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { normalizeCookies, exportCookies } from "../tools/cookie-extension/cookies.mjs";

test("scope, expiry, HttpOnly and domain-only identity survive export", () => {
  const sample = { name: "unb", value: "test", domain: ".taobao.com", path: "/", session: true, httpOnly: true };
  const out = normalizeCookies([
    sample, sample,
    { ...sample, domain: "taobao.com.evil.example" },
    { ...sample, domain: ".tmall.com", name: "tmall" },
    { ...sample, domain: "login.taobao.com", name: "host", sameSite: "strict" },
    { ...sample, name: "expired", session: false, expirationDate: 99 },
    { ...sample, name: "partitioned", partitionKey: { topLevelSite: "https://tmall.com" } },
  ], 100);
  assert.equal(out.state.cookies.length, 4);
  assert.equal(out.skipped, 1);
  assert.equal(out.state.cookies[0].httpOnly, true);
  assert.equal(out.state.cookies[2].domain, "login.taobao.com");
  assert.equal(out.state.cookies[2].sameSite, "Strict");
  assert.deepEqual(out.state.origins, []);
});

test("reads only Taobao/Tmall stores and propagates failure without empty export", async () => {
  const calls = [];
  const api = { cookies: { getAll: async ({ domain }) => { calls.push(domain); return []; } } };
  await assert.rejects(exportCookies(api), /未找到可用/);
  assert.deepEqual([...new Set(calls)].sort(), ["taobao.com", "tmall.com"]);
  await assert.rejects(exportCookies({ cookies: { getAll: async () => { throw Error("permission"); } } }), /permission/);
});

test("partition identity and ancestor flags never overwrite each other", () => {
  const base = { name: "auth", value: "fixture", domain: ".taobao.com", session: true };
  const out = normalizeCookies([base,
    { ...base, partitionKey: { topLevelSite: "https://tmall.com", hasCrossSiteAncestor: true } },
    { ...base, partitionKey: { topLevelSite: "https://tmall.com", hasCrossSiteAncestor: false } },
    { ...base, partitionKey: { topLevelSite: "https://evil.example" } },
  ]);
  assert.equal(out.state.cookies.length, 3);
  assert.equal(out.skipped, 1);
  assert.equal(out.state.cookies[1].partitionKey, "https://tmall.com");
  assert.equal(out.state.cookies[2]._crHasCrossSiteAncestor, false);
  assert.equal(out.state.cookies[1].httpOnly, false);
});

test("exports current-store partitioned cookies and localStorage without mixing windows", async () => {
  const calls = [];
  const api = {
    tabs: { query: async (q) => q.active ? [{ id: 20, incognito: false }] : [
      { id: 20, url: "https://detail.tmall.com/item.htm?id=123", incognito: false },
      { id: 30, url: "https://www.taobao.com/", incognito: false },
      { id: 40, url: "https://www.taobao.com/", incognito: true },
      { id: 50, url: "https://evil.example", incognito: false },
    ] },
    cookies: {
      getAllCookieStores: async () => [{ id: "store0", tabIds: [20] }, { id: "private", tabIds: [40] }],
      getAll: async (q) => {
        calls.push(q);
        if (q.domain !== "taobao.com") return [];
        return [{ name: "unb", value: "fixture", domain: ".taobao.com", session: true, ...(q.partitionKey ? { partitionKey: q.partitionKey } : {}) }];
      },
    },
    scripting: { executeScript: async ({ target }) => [{ result: {
      origin: target.tabId === 20 ? "https://detail.tmall.com" : "https://www.taobao.com",
      localStorage: [{ name: "fixture", value: "local" }],
    } }] },
  };
  const out = await exportCookies(api);
  assert.ok(calls.every((q) => q.storeId === "store0"));
  assert.equal(calls.filter((q) => q.partitionKey).length, 8);
  assert.equal(out.state.origins.length, 2);
  assert.equal(out.state.export_info.partitioned_cookie_count, 4);
  assert.equal(out.state.export_info.version, "1.2.0");
  assert.equal(out.state.export_info.storage_read_failures, 0);
});

test("reads login iframes even in duplicate-origin tabs, without reading foreign frames", async () => {
  const targets = [];
  let foreignReads = 0;
  const out = await exportCookies({
    tabs: { query: async (q) => q.active ? [{ id: 1, incognito: false }] : [
      { id: 1, active: true, incognito: false, url: "https://www.taobao.com/" },
      { id: 2, incognito: false, url: "https://www.taobao.com/" },
    ] },
    cookies: { getAll: async () => [{ name: "unb", value: "fixture", domain: ".taobao.com", session: true }] },
    scripting: { executeScript: async ({ target, func }) => {
      targets.push(target);
      const urls = ["https://www.taobao.com", target.tabId === 1 ? "https://login.taobao.com" : "https://passport.taobao.com", "https://taobao.com.evil.example"];
      return urls.map((url) => ({ result: vm.runInNewContext(`(${func.toString()})()`, {
        location: new URL(url),
        get localStorage() {
          if (url.endsWith("evil.example")) { foreignReads++; throw Error("unrelated-site-secret"); }
          const entries = { fixture: `tab-${target.tabId}` };
          return new Proxy(entries, { get: (obj, name) => name === "getItem" ? (key) => obj[key] : obj[name] });
        },
      }) }));
    } },
  });
  assert.deepEqual(targets, [{ tabId: 1, allFrames: true }, { tabId: 2, allFrames: true }]);
  assert.deepEqual(out.state.origins.map((x) => x.origin).sort(), ["https://login.taobao.com", "https://passport.taobao.com", "https://www.taobao.com"]);
  assert.equal(out.state.origins.find((x) => x.origin === "https://www.taobao.com").localStorage[0].value, "tab-1");
  assert.equal(out.state.export_info.storage_frames_read, 4);
  assert.equal(out.state.export_info.storage_read_failures, 0);
  assert.deepEqual(out.state.export_info.missing_auth_storage_origins, []);
  assert.equal(foreignReads, 0);
});

test("blocked iframe storage is reported without discarding main-page storage", async () => {
  const out = await exportCookies({
    tabs: { query: async (q) => q.active ? [{ id: 1, incognito: false }] : [{ id: 1, incognito: false, url: "https://www.taobao.com/" }] },
    cookies: { getAll: async () => [{ name: "unb", value: "fixture", domain: ".taobao.com", session: true }] },
    scripting: { executeScript: async () => [
      { result: { origin: "https://www.taobao.com", localStorage: [] } },
      { result: { origin: "https://login.taobao.com", localStorage: null } },
    ] },
  });
  assert.equal(out.state.export_info.storage_read_failures, 1);
  assert.equal(out.state.export_info.storage_frames_read, 1);
  assert.deepEqual(out.state.export_info.missing_auth_storage_origins, ["https://login.taobao.com", "https://passport.taobao.com"]);
  assert.equal(out.state.origins.length, 1);
});

test("storage failures are reported rather than described as complete session", async () => {
  const out = await exportCookies({
    tabs: { query: async (q) => q.active ? [{ id: 1, incognito: false }] : [{ id: 1, incognito: false, url: "https://www.taobao.com/" }] },
    cookies: { getAll: async () => [{ name: "test", value: "fixture", domain: ".taobao.com", session: true }] },
    scripting: { executeScript: async () => { throw Error("fixture-secret"); } },
  });
  assert.deepEqual(out.state.origins, []);
  assert.equal(out.state.export_info.storage_read_failures, 1);
  assert.equal(JSON.stringify(out.state.export_info).includes("fixture-secret"), false);
});

test("export click creates upload-ready download and handles blocked permission", async () => {
  let click;
  const button = { disabled: false, addEventListener: (_type, handler) => { click = handler; } };
  const status = { dataset: {}, textContent: "" };
  const downloads = [];
  globalThis.document = { querySelector: (s) => s === "#export" ? button : status };
  globalThis.chrome = {
    cookies: { getAll: async ({ domain }) => domain === "taobao.com" ? [{ name: "unb", value: "fixture", domain: ".taobao.com", session: true, httpOnly: true }] : [] },
    downloads: { download: async (options) => {
      // Reading this locally-created blob is test-only; extension has no fetch.
      const result = await (await fetch(options.url)).json();
      downloads.push({ options, result });
      return 1;
    } },
  };
  const originalTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, delay) => { const timer = originalTimeout(fn, delay); timer.unref(); return timer; };
  try {
    await import("../tools/cookie-extension/popup.js");
    await click();
    assert.equal(downloads.length, 1);
    assert.equal(downloads[0].result.cookies[0].httpOnly, true);
    assert.equal(downloads[0].options.saveAs, true);
    assert.match(downloads[0].options.filename, /^taobao-cookie-.*\.json$/);
    assert.equal(button.disabled, false);
    assert.doesNotMatch(status.textContent, /fixture/);
    chrome.cookies.getAll = async () => { throw Error("fixture-secret"); };
    await click();
    assert.equal(status.dataset.error, "true");
    assert.equal(button.disabled, false);
    assert.doesNotMatch(status.textContent, /fixture-secret/);
    assert.equal(downloads.length, 1);
  } finally { globalThis.setTimeout = originalTimeout; }
});
