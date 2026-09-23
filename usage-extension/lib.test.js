import { test } from "node:test";
import assert from "node:assert/strict";
import {
  USAGE_URL_PATTERNS,
  PROXY_INGEST_URL,
  orgIdFrom,
  shouldCapture,
  makeDebouncer,
  envelope,
  usageUrl,
} from "./lib.js";

const ORG = "0b5e8f2a-1c3d-4e5f-8a9b-0c1d2e3f4a5b";
const PLAIN = `https://claude.ai/api/organizations/${ORG}/usage`;
const CEDAR = `${PLAIN}?cedar_ember=1&skip_spend=1`;

test("orgIdFrom returns the org id for the usage URL", () => {
  assert.equal(orgIdFrom(PLAIN), ORG);
});

test("orgIdFrom accepts the cedar_ember query variant", () => {
  assert.equal(orgIdFrom(CEDAR), ORG);
});

test("orgIdFrom rejects other paths under the org", () => {
  for (const url of [
    `${PLAIN}/foo`,
    `${PLAIN}foo`,
    `https://claude.ai/api/organizations/${ORG}`,
    `https://claude.ai/api/organizations/${ORG}/chat_conversations`,
    `https://claude.ai/api/organizations/${ORG}/projects/x/usage`,
    "https://claude.ai/api/organizations//usage",
    "https://claude.ai/api/organizations",
    "https://claude.ai/settings/usage",
  ]) {
    assert.equal(orgIdFrom(url), null, url);
  }
});

test("orgIdFrom rejects hosts other than https://claude.ai", () => {
  for (const url of [
    `http://claude.ai/api/organizations/${ORG}/usage`,
    `https://claude.ai.evil.com/api/organizations/${ORG}/usage`,
    `https://evil.com/api/organizations/${ORG}/usage`,
    `https://api.claude.ai/api/organizations/${ORG}/usage`,
    `https://claude.ai@evil.com/api/organizations/${ORG}/usage`,
    `https://claude.ai:8443/api/organizations/${ORG}/usage`,
    `https://evil.com/?u=https://claude.ai/api/organizations/${ORG}/usage`,
  ]) {
    assert.equal(orgIdFrom(url), null, url);
  }
});

test("orgIdFrom rejects org ids that could escape the path", () => {
  for (const id of ["..", "a%2Fb", "a b", "%2e%2e"]) {
    assert.equal(orgIdFrom(`https://claude.ai/api/organizations/${id}/usage`), null, id);
  }
});

test("orgIdFrom uses the proxy's org-id rule, [A-Za-z0-9_-]{1,64}", () => {
  const at = (id) => orgIdFrom(`https://claude.ai/api/organizations/${id}/usage`);
  assert.equal(at("a".repeat(64)), "a".repeat(64));
  assert.equal(at("a".repeat(65)), null);
  assert.equal(at("org_with_underscore"), "org_with_underscore");
});

test("orgIdFrom returns null for malformed input", () => {
  for (const url of [undefined, null, "", "not a url", 42, {}, "claude.ai/api/organizations/x/usage"]) {
    assert.equal(orgIdFrom(url), null, String(url));
  }
});

const pageRequest = (over = {}) => ({
  url: PLAIN,
  method: "GET",
  statusCode: 200,
  tabId: 7,
  initiator: "https://claude.ai",
  ...over,
});

test("shouldCapture accepts a GET 200 from a tab, both usage variants", () => {
  assert.equal(shouldCapture(pageRequest()), true);
  assert.equal(shouldCapture(pageRequest({ url: CEDAR })), true);
  assert.equal(shouldCapture(pageRequest({ tabId: 0 })), true);
});

test("shouldCapture rejects the extension's own fetch (tabId -1)", () => {
  assert.equal(shouldCapture(pageRequest({ tabId: -1 })), false);
});

test("shouldCapture requires the request to be initiated by https://claude.ai", () => {
  for (const initiator of [undefined, "https://evil.com", "chrome-extension://abcdefghijklmnop", "http://claude.ai", "https://claude.ai.evil.com"]) {
    assert.equal(shouldCapture(pageRequest({ initiator })), false, String(initiator));
  }
});

test("shouldCapture rejects non-200, non-GET and non-matching URLs", () => {
  for (const statusCode of [0, 204, 304, 401, 403, 429, 500]) {
    assert.equal(shouldCapture(pageRequest({ statusCode })), false, String(statusCode));
  }
  for (const method of ["POST", "HEAD", "OPTIONS", "get"]) {
    assert.equal(shouldCapture(pageRequest({ method })), false, method);
  }
  assert.equal(shouldCapture(pageRequest({ url: `${PLAIN}/foo` })), false);
  assert.equal(shouldCapture(pageRequest({ url: "https://evil.com/api/organizations/x/usage" })), false);
});

test("shouldCapture returns false for missing or malformed details", () => {
  for (const d of [undefined, null, {}, { url: PLAIN }, pageRequest({ tabId: undefined })]) {
    assert.equal(shouldCapture(d), false, JSON.stringify(d));
  }
});

test("debouncer allows one capture per org per 60 s", () => {
  let t = 1_000_000;
  const allow = makeDebouncer({ now: () => t });
  assert.equal(allow("a"), true);
  assert.equal(allow("a"), false);
  t += 59_999;
  assert.equal(allow("a"), false);
  t += 1;
  assert.equal(allow("a"), true);
  assert.equal(allow("a"), false);
});

test("debouncer tracks orgs independently", () => {
  const t = 5;
  const allow = makeDebouncer({ now: () => t });
  assert.equal(allow("a"), true);
  assert.equal(allow("b"), true);
  assert.equal(allow("a"), false);
  assert.equal(allow("b"), false);
});

test("debouncer interval is configurable and suppressed calls don't extend it", () => {
  let t = 0;
  const allow = makeDebouncer({ intervalMs: 10, now: () => t });
  assert.equal(allow("a"), true);
  t = 9;
  assert.equal(allow("a"), false);
  t = 10;
  assert.equal(allow("a"), true);
});

test("envelope has org_id, ISO-8601 UTC observed_at and the untouched payload", () => {
  const payload = { five_hour: { utilization: 3 }, seven_day_breakdown: { claude_code: 79 } };
  const env = envelope(ORG, payload, Date.UTC(2026, 8, 23, 5, 6, 7, 890));
  assert.deepEqual(Object.keys(env).sort(), ["observed_at", "org_id", "payload"]);
  assert.equal(env.org_id, ORG);
  assert.equal(env.observed_at, "2026-09-23T05:06:07.890Z");
  assert.equal(env.payload, payload);
});

test("usageUrl builds the plain usage URL, which orgIdFrom round-trips", () => {
  assert.equal(usageUrl(ORG), PLAIN);
  assert.equal(orgIdFrom(usageUrl(ORG)), ORG);
});

test("proxy ingest URL is the local claude-usage endpoint", () => {
  assert.equal(PROXY_INGEST_URL, "http://127.0.0.1:8787/__proxy/claude-usage");
});

// Chrome match patterns: in the path part, `*` matches any run of characters,
// and webRequest matches the path pattern against path plus query string.
function matchPatternRegex(pattern) {
  const [, scheme, host, path] = pattern.match(/^(https?):\/\/([^/]+)(\/.*)$/);
  const esc = (s) => s.replace(/[.+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^${scheme}://${esc(host)}${esc(path).replace(/\*/g, ".*")}$`);
}

test("webRequest filter patterns cover both usage variants and nothing else under the org API", () => {
  const res = USAGE_URL_PATTERNS.map(matchPatternRegex);
  const matches = (url) => res.some((re) => re.test(url));
  assert.ok(matches(PLAIN));
  assert.ok(matches(CEDAR));
  for (const url of [
    `https://claude.ai/api/organizations/${ORG}/chat_conversations`,
    "https://claude.ai/settings/usage",
    `http://claude.ai/api/organizations/${ORG}/usage`,
    `https://claude.ai/api/organizations/${ORG}/usage_limits`,
  ]) {
    assert.ok(!matches(url), url);
  }
  // The org `*` can span `/` and `?`, so no pattern keeping it excludes this;
  // it wakes the worker and shouldCapture rejects it.
  const sneaky = `https://claude.ai/api/organizations/${ORG}/search?q=/usage`;
  assert.ok(matches(sneaky));
  assert.equal(shouldCapture(pageRequest({ url: sneaky })), false);
});
