# Plan: claude.ai usage-page capture extension

## Goal

Whenever a claude.ai page fetches the usage endpoint (the settings/usage
page does, and so does the app shell), record the usage JSON, especially `seven_day_breakdown` (Claude Code / Cowork / Chat), in the
proxy's database. The page must load exactly as it does now. No other
claude.ai traffic may be touched, delayed or read.

## Critique of the suggested shape

The earlier suggestion was an MV3 service worker on a `chrome.alarms` timer.
It fetches `/api/organizations/<org>/usage` using the browser's cookies, gets
the org id from `/api/organizations` on first run, and POSTs the result to a
new endpoint on the proxy. Three parts of that are right: using the browser's
own TLS and cookies (the spike showed that Cloudflare challenges the Python
client), sending the raw JSON to the proxy, and doing the parsing in Python.
Three parts should change:

1. **The trigger is wrong for this brief.** A timer polls claude.ai with
   Victor's session every N minutes whether or not anyone asked, and it
   doesn't capture anything when the page is opened. Use the page view itself
   as the trigger. A timer can come later as an opt-in (see "Later").
2. **Don't hook into the page.** The alternative, reading the page's own
   response, needs a MAIN-world content script that wraps `window.fetch` on
   claude.ai. That script runs inside every claude.ai request and could break
   the page. MV3 `webRequest` can't read response bodies, so there is no clean
   passive way to get the body.
3. **The org-id bootstrap isn't needed.** The page's own request URL already
   contains the org id.

## Design

**Observe, then re-fetch.**

1. The extension registers a non-blocking
   `chrome.webRequest.onCompleted` listener, filtered to
   `https://claude.ai/api/organizations/*/usage`. A non-blocking listener can
   only watch. It cannot change, delay or cancel the request, so the page
   loads exactly as before, and no other URL wakes the extension.
2. When that request completes with status 200 from a tab (`tabId >= 0`),
   the worker takes the org id from the URL and runs its own
   `fetch(url, {credentials: "include"})`. That costs one extra GET per page
   view. The worker's own fetch has `tabId === -1`, so the listener ignores it
   and can't loop.
3. The worker POSTs `{org_id, observed_at, payload}` as `application/json` to
   `http://127.0.0.1:8787/__proxy/claude-usage`. If the proxy is down, the
   extension logs to the worker console and drops the data. It does no retry
   or queuing, because the next page view sends fresh data anyway.

Manifest permissions: `webRequest` only, no `webRequestBlocking`, `scripting`,
`tabs`, `alarms` or content scripts. Host permissions: `https://claude.ai/*`
and `http://127.0.0.1:8787/*`.

### Proxy side

- Port only the recording part of the spike (03f19e4) onto main:
  `parse_claude_usage`, the `claude_usage` table plus schema, the
  `Recorder.claude_usage` writer with `USAGE_DEDUP_ON`, and the
  `_print_claude_usage` section in `status`. Leave out the multi-upstream
  routing, the SAN reissue and the hosts step. The branch stays abandoned.
- Add `POST /__proxy/claude-usage` next to `/__proxy/health`. It already sits
  under the prefix that is answered locally and never relayed, so it can't
  leak upstream.
- **Guard the endpoint.** Any web page Victor visits can send a no-preflight
  POST to 127.0.0.1. The endpoint should:
  - require `Content-Type: application/json`, so a cross-origin page triggers
    a preflight, which the proxy never approves;
  - require an `Origin` header starting with `chrome-extension://`, which a
    web page can't forge;
  - send no CORS headers;
  - reject bodies over 256 KB;
  - reject payloads that `parse_claude_usage` rejects;
  - respond 204 when a row is written or deduplicated, and 4xx otherwise.
- `TLSProxy` inherits the route. That's harmless, but the plan limits
  `/__proxy/claude-usage` to the plain listener so the TLS listener only ever
  serves traffic bound for api.anthropic.com.

## Spike results (23 Sep 2026)

A throwaway extension logged to a local receiver while the usage page was
opened in two tabs.

- **The re-fetch works.** A service-worker `fetch(url, {credentials:
  "include"})` returned 200 `application/json` with no Cloudflare challenge,
  including `seven_day_breakdown`: Claude Code 79, Chats 2, Cowork 19,
  Other 0, with the window starting 2026-09-22T15:00Z.
- **The loop guard holds.** The page's requests arrive with a real `tabId`.
  The worker's own fetch arrives with `tabId -1` and
  `initiator: chrome-extension://<id>`.
- **The POST to 127.0.0.1 is fine.** It carried
  `Origin: chrome-extension://<id>` and was not preflighted, because host
  permission bypasses CORS. The Origin check in the plan will work.
- **The page calls `/usage` several times per load.** It called plain
  `/usage` two or three times, plus `/usage?cedar_ember=1&skip_spend=1`,
  which has the same top-level keys and is 216 bytes bigger. The claude.ai
  app shell seems to fetch it too, not only `/settings/usage`.

Plan changes that follow from the spike:

- **Debounce.** At most one re-fetch per org every 60 s, tracked in memory in
  the worker. A worker restart costing one extra fetch is fine, because the
  proxy dedupes.
- **Re-fetch a URL built from the org id.** Always fetch plain
  `/api/organizations/<org>/usage`, whichever variant the page used. That is
  the shape the spike parser knows. `shouldCapture` matches both variants.
- **Scope: any claude.ai page.** Victor decided this on 23 Sep. The endpoint
  is also fetched outside the usage page, so the extension captures whenever
  any claude.ai tab fetches it. There is no tab-URL check and no `tabs` API
  use.

## Layout

```
usage-extension/
  manifest.json     MV3, permissions above
  background.js     chrome.* glue only: listener, fetch, post
  lib.js            pure functions: orgIdFrom(url), shouldCapture(details),
                    envelope(orgId, payload, now)
  lib.test.js       node --test, no dependencies
  README.md         load unpacked, verify with `proxy.py status`
```

## Build order (TDD throughout)

Each step starts with a failing test that has been run and seen to fail.

1. **Spike check.** Done and passed. See "Spike results".
2. **Parser and table.** Port the spike's
   `test_parse_claude_usage_rejects_unrelated_json` and the parsing half of
   `test_claude_usage_page_is_recorded_and_deduped`, rewritten so they don't
   rely on host routing. Then port the code.
3. **Ingest endpoint.** Tests against the `plain_server` fixture:
   - a valid POST writes a row;
   - an identical second POST is deduplicated;
   - a missing or `http(s)://` Origin gets 403;
   - a `text/plain` body gets 415;
   - an unrelated JSON body gets 422;
   - an oversize body gets 413;
   - a GET gets 405;
   - nothing reaches the upstream stub.
4. **Status output.** A test that `status` prints the latest by-surface
   split.
5. **Extension pure functions.** `node --test` tests:
   - `orgIdFrom` returns the org id for the usage URL and `null` for
     `/usage/foo`, other orgs endpoints and non-claude.ai hosts;
   - `shouldCapture` accepts a 200 from a tab and rejects `tabId -1`, non-200
     and non-GET;
   - `shouldCapture` accepts both the plain `/usage` URL and the
     `?cedar_ember=1&skip_spend=1` variant;
   - a debounce helper allows one capture per org per 60 s;
   - `envelope` has the expected shape.
6. **Glue plus manual end-to-end.** Write `background.js` and
   `manifest.json`, load unpacked, open the usage page, confirm that it
   renders normally, that `status` shows the split, and that the service
   worker console is clean. Open chat and Claude Code pages and confirm the
   worker never wakes.
7. **Docs.** Add a README for the extension and a short section in the main
   README, and update the memory note on the usage endpoint.

## Later (not in this plan)

- An opt-in `chrome.alarms` poll (for example every 6 h, or only while a
  claude.ai tab is open), so the table fills without remembering to open the
  page.
- Using the stored breakdown in `calibrate` to scale the proxy's floor
  estimates by the `claude_code` share.
