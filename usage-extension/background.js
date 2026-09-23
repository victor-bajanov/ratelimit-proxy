// chrome.* glue: watch the page's usage request, re-fetch it with the
// browser's cookies, and POST it to the local proxy. Failures are logged and
// dropped; the next page view sends fresh data.
import {
  USAGE_URL_PATTERNS,
  PROXY_INGEST_URL,
  orgIdFrom,
  shouldCapture,
  makeDebouncer,
  envelope,
  usageUrl,
} from "./lib.js";

const allow = makeDebouncer();

export async function capture(orgId) {
  try {
    const res = await fetch(usageUrl(orgId), { credentials: "include" });
    if (res.status !== 200) throw new Error(`usage fetch returned ${res.status}`);
    const payload = await res.json();
    const post = await fetch(PROXY_INGEST_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(envelope(orgId, payload, Date.now())),
    });
    if (!post.ok) throw new Error(`proxy returned ${post.status}`);
  } catch (err) {
    console.warn(`usage capture for ${orgId} dropped:`, err);
  }
}

function onCompleted(details) {
  try {
    if (!shouldCapture(details)) return;
    const orgId = orgIdFrom(details.url);
    // A failed capture still uses up the window, on purpose: no retries.
    if (allow(orgId)) capture(orgId);
  } catch (err) {
    console.warn("usage listener error:", err);
  }
}

// Non-blocking (no extraInfoSpec), registered synchronously at top level so
// the MV3 worker is woken for these events.
chrome.webRequest.onCompleted.addListener(onCompleted, { urls: USAGE_URL_PATTERNS });
