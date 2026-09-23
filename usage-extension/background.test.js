import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { USAGE_URL_PATTERNS, PROXY_INGEST_URL } from "./lib.js";

// background.js registers its listener at import time, so install fakes
// first and import once. Each test uses its own org id to sidestep the
// module-level debouncer.
const registered = [];
globalThis.chrome = {
  webRequest: { onCompleted: { addListener: (fn, filter, extra) => registered.push({ fn, filter, extra }) } },
};
let fetchCalls;
let fetchImpl;
globalThis.fetch = (url, init) => {
  fetchCalls.push({ url, init });
  return fetchImpl(url, init);
};
const warnings = [];
console.warn = (...args) => warnings.push(args.join(" "));

const { capture } = await import("./background.js");
const [{ fn: listener, filter, extra }] = registered;

const jsonResponse = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const PAYLOAD = {
  five_hour: { utilization: 1 },
  seven_day_breakdown: {
    as_of: "2026-09-23T05:09:18.238571+00:00",
    window_started_at: "2026-09-22T15:00:00.216686+00:00",
    rows: [
      { key: "claude_code", display_name: "Claude Code", percent: 79 },
      { key: "chat", display_name: "Chats", percent: 2 },
      { key: "cowork", display_name: "Cowork", percent: 19 },
      { key: "other", display_name: "Other", percent: 0 },
    ],
  },
};
const details = (org, over = {}) => ({
  url: `https://claude.ai/api/organizations/${org}/usage?cedar_ember=1&skip_spend=1`,
  method: "GET",
  statusCode: 200,
  tabId: 3,
  initiator: "https://claude.ai",
  ...over,
});

beforeEach(() => {
  fetchCalls = [];
  warnings.length = 0;
  fetchImpl = (url) => Promise.resolve(url === PROXY_INGEST_URL ? new Response(null, { status: 204 }) : jsonResponse(PAYLOAD));
});

test("registers exactly one non-blocking onCompleted listener filtered to the usage pattern", () => {
  assert.equal(registered.length, 1);
  assert.deepEqual(filter, { urls: USAGE_URL_PATTERNS });
  assert.equal(extra, undefined);
});

test("capture re-fetches the plain usage URL with cookies and POSTs the envelope as JSON", async () => {
  await capture("org-1");
  assert.equal(fetchCalls.length, 2);
  const [get, post] = fetchCalls;
  assert.equal(get.url, "https://claude.ai/api/organizations/org-1/usage");
  assert.equal(get.init.credentials, "include");
  assert.equal(post.url, PROXY_INGEST_URL);
  assert.equal(post.init.method, "POST");
  assert.equal(post.init.headers["Content-Type"], "application/json");
  const body = JSON.parse(post.init.body);
  assert.equal(body.org_id, "org-1");
  assert.deepEqual(body.payload, PAYLOAD);
  assert.match(body.observed_at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
});

test("capture drops non-200 usage responses without posting", async () => {
  fetchImpl = () => Promise.resolve(jsonResponse({ error: "nope" }, 403));
  await capture("org-2");
  assert.equal(fetchCalls.length, 1);
  assert.equal(warnings.length, 1);
});

test("capture drops non-JSON usage responses without posting", async () => {
  fetchImpl = () => Promise.resolve(new Response("<html>challenge</html>", { status: 200 }));
  await capture("org-3");
  assert.equal(fetchCalls.length, 1);
  assert.equal(warnings.length, 1);
});

test("capture logs when the usage fetch rejects, and resolves", async () => {
  fetchImpl = () => Promise.reject(new TypeError("Failed to fetch"));
  await capture("org-4");
  assert.equal(fetchCalls.length, 1);
  assert.equal(warnings.length, 1);
});

test("capture logs when the proxy is down or refuses, and resolves", async () => {
  fetchImpl = (url) => (url === PROXY_INGEST_URL ? Promise.reject(new TypeError("Failed to fetch")) : Promise.resolve(jsonResponse(PAYLOAD)));
  await capture("org-5");
  assert.equal(fetchCalls.length, 2);
  assert.equal(warnings.length, 1);

  warnings.length = 0;
  fetchImpl = (url) => Promise.resolve(url === PROXY_INGEST_URL ? new Response("bad", { status: 422 }) : jsonResponse(PAYLOAD));
  await capture("org-5b");
  assert.equal(warnings.length, 1);
});

const settle = () => new Promise((r) => setTimeout(r, 0));

test("listener captures a page request once, then debounces the same org", async () => {
  listener(details("org-6"));
  await settle();
  assert.deepEqual(fetchCalls.map((c) => c.url), ["https://claude.ai/api/organizations/org-6/usage", PROXY_INGEST_URL]);
  listener(details("org-6"));
  await settle();
  assert.equal(fetchCalls.length, 2);
});

test("listener ignores rejected requests without using up the org's window", async () => {
  const rejected = {
    "org-7a": { tabId: -1 },
    "org-7b": { statusCode: 500 },
    "org-7c": { url: "https://claude.ai/api/organizations/org-7c/usage/foo" },
    "org-7d": { initiator: "https://evil.com" },
  };
  for (const [org, over] of Object.entries(rejected)) {
    listener(details(org, over));
    await settle();
    assert.equal(fetchCalls.length, 0, org);
    listener(details(org));
    await settle();
    assert.deepEqual(fetchCalls.map((c) => c.url), [`https://claude.ai/api/organizations/${org}/usage`, PROXY_INGEST_URL], org);
    fetchCalls = [];
  }
});

test("listener never throws, even on garbage or a synchronously throwing fetch", async () => {
  assert.doesNotThrow(() => listener(undefined));
  assert.doesNotThrow(() => listener({ url: 42 }));
  fetchImpl = () => {
    throw new Error("boom");
  };
  assert.doesNotThrow(() => listener(details("org-8")));
  await settle();
  assert.equal(warnings.length, 1);
});
