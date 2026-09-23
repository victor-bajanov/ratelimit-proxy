// Pure helpers for the usage-capture extension. No chrome.* here, so node
// can test it; background.js imports it as an ES module.

// webRequest matches the path pattern against path plus query, so plain
// `/usage` and `/usage?cedar_ember=1&skip_spend=1` need a pattern each. The
// `*` for the org can still span slashes; shouldCapture narrows that.
export const USAGE_URL_PATTERNS = [
  "https://claude.ai/api/organizations/*/usage",
  "https://claude.ai/api/organizations/*/usage?*",
];
export const PROXY_INGEST_URL = "http://127.0.0.1:8787/__proxy/claude-usage";
export const DEBOUNCE_MS = 60_000;

// The org id rule matches the proxy's ORG_ID, so nothing sent is refused there.
const USAGE_PATH = /^\/api\/organizations\/([A-Za-z0-9_-]{1,64})\/usage$/;

// The org id from a claude.ai usage URL (query string allowed), else null.
export function orgIdFrom(url) {
  if (typeof url !== "string") return null;
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (u.protocol !== "https:" || u.host !== "claude.ai" || u.username || u.password) return null;
  const m = u.pathname.match(USAGE_PATH);
  return m ? m[1] : null;
}

// True for a completed GET of the usage endpoint made by a claude.ai page.
// The initiator check stops another site's no-cors fetch from triggering a
// credentialed re-fetch; the tabId check skips the extension's own fetch.
export function shouldCapture(details) {
  return (
    details != null &&
    details.initiator === "https://claude.ai" &&
    details.method === "GET" &&
    details.statusCode === 200 &&
    Number.isInteger(details.tabId) &&
    details.tabId >= 0 &&
    orgIdFrom(details.url) !== null
  );
}

// Returns allow(orgId): true at most once per org per intervalMs. In memory
// only, and Chrome stops an idle MV3 worker after about 30 s, so in practice
// this collapses the burst of usage requests within one page load.
export function makeDebouncer({ intervalMs = DEBOUNCE_MS, now = Date.now } = {}) {
  const last = new Map();
  return (orgId) => {
    const t = now();
    if (last.has(orgId) && t - last.get(orgId) < intervalMs) return false;
    last.set(orgId, t);
    return true;
  };
}

// observed_at is an ISO-8601 UTC string with milliseconds, e.g.
// "2026-09-23T05:06:07.890Z". `now` is epoch ms (or a Date).
export function envelope(orgId, payload, now) {
  return { org_id: orgId, observed_at: new Date(now).toISOString(), payload };
}

export function usageUrl(orgId) {
  return `https://claude.ai/api/organizations/${orgId}/usage`;
}
