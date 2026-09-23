# usage-extension

A Chrome (MV3) extension that copies claude.ai's usage JSON into the proxy's
database. The main prize is `seven_day_breakdown`, the week's usage split by
surface (Claude Code, Chats, Cowork, Other). The `anthropic-ratelimit-unified-*`
API headers don't carry it.

## What it does

It observes, then re-fetches.

1. A non-blocking `chrome.webRequest.onCompleted` listener watches
   `https://claude.ai/api/organizations/*/usage` and `.../usage?*`. Being
   non-blocking, it can't change, delay or cancel a request, so the page loads
   exactly as before. Chrome only wakes the worker for URLs that match those
   two patterns.
2. When a claude.ai page's GET for that URL completes with a 200, the worker
   takes the org id from the URL. It then re-fetches the plain
   `/api/organizations/<org>/usage` with the browser's own cookies. The page
   calls the endpoint several times per load, so this happens at most once per
   org per 60 s. The debounce is in memory, and Chrome stops an idle worker
   after about 30 s, so in practice it collapses the burst within one page
   load. Expect about one extra GET per page view.
3. It POSTs `{org_id, observed_at, payload}` to
   `http://127.0.0.1:8787/__proxy/claude-usage`. Nothing is sent anywhere
   else.

It ignores:
- its own re-fetch, which has `tabId -1`, so it can't loop;
- requests started by any origin other than `https://claude.ai`;
- non-GET requests and anything other than a 200;
- other paths such as `/usage/foo` or `/usage_limits`.

It has no content scripts. It doesn't read page responses, and it never
touches any other claude.ai traffic.

## Permissions

| Permission | Why |
| --- | --- |
| `webRequest` | Lets the extension see that the page's usage request completed. It can only watch, since there is no `webRequestBlocking`. |
| `https://claude.ai/*` | Needed for the webRequest events, and for the re-fetch to send the session cookies. |
| `http://127.0.0.1:8787/*` | Lets it POST to the local proxy. |

It doesn't use `tabs`, `scripting`, `alarms` or `storage`, and it has no content
scripts. `manifest.test.js` checks all of this.

Port 8787 is hard-coded in two places: `PROXY_INGEST_URL` in `lib.js` and the
host permission in `manifest.json`. If the proxy runs with a different
`--port`, change both, and the matching tests.

## Load it

1. Open `chrome://extensions` and turn on Developer mode.
2. Click Load unpacked and pick this folder.

To pick up edits, click the reload arrow on the extension's card.

## Tests

```
cd usage-extension
node --test
```

This needs Node with the built-in test runner (tested on v26). There are no
npm dependencies. `node --test usage-extension/` from the repo root doesn't
work, because node tries to load the directory as a module. From the repo root,
use `node --test usage-extension/*.test.js` instead.

- `lib.js`: the pure logic (URL matching, `shouldCapture`, debounce,
  envelope), tested by `lib.test.js`.
- `background.js`: the `chrome.*` glue, tested in `background.test.js` against
  a fake `chrome` and `fetch`.

## Verify

1. Make sure the running proxy includes the `/__proxy/claude-usage` endpoint.
   An older build answers that path with 404. Restart the launchd agent after
   updating `proxy.py`.
2. Open any claude.ai page. The app shell fetches the usage endpoint, and so
   does Settings → Usage.
3. Run `uv run proxy.py status`. The end of the output should look something
   like this:

   ```
   latest claude.ai usage (Wed 23 Sep 15:40): 5h …%  7d …%  7d <model> …%
     this week by surface: Claude Code 79%, Chats 2%, Cowork 19%, Other 0%
   ```

4. On `chrome://extensions`, click the extension's "service worker" link to
   open its console. When a capture works, the console shows nothing. When one
   fails, it shows a `usage capture for <org> dropped:` warning.

## Failure behaviour

Any failure is logged to the worker console and the data is dropped. There
are no retries and no queue, because the next page view sends fresh data.
That covers:
- the proxy being down, or answering with anything other than 2xx (including
  404 from an older build);
- the re-fetch failing or returning something other than a 200;
- a response body that isn't JSON.

A failed capture still uses up that org's 60 s window.

## Proxy-side contract

`POST /__proxy/claude-usage`, on the plain listener (`127.0.0.1:8787`) only.
The TLS listener answers 404 for this path.

Request:
- `Origin` must match `chrome-extension://[a-p]{32}`. Chrome sets this, and a
  web page can neither forge nor omit its own. The check accepts any
  extension id, though. Another installed extension with host access to
  127.0.0.1 (or `<all_urls>`) could plant false usage rows. It couldn't read
  anything back, and nothing is relayed. That's accepted for now. A possible
  later hardening step is to pin this extension's id with a manifest `key`
  and have the proxy check for it.
- `Content-Type: application/json`, so a cross-origin page would be
  preflighted. The proxy never approves preflights and sends no CORS headers.
- A `Content-Length` of at most 256 KB. Chunked bodies are refused.
- The body is the envelope:

  ```json
  {"org_id": "<org uuid>",
   "observed_at": "2026-09-23T05:06:07.890Z",
   "payload": { "...": "the usage JSON, untouched" }}
  ```

  - `org_id` must match `[A-Za-z0-9_-]{1,64}`.
  - `observed_at` is an ISO 8601 string with a timezone (the extension sends
    `Date.prototype.toISOString()`), and must be within a day of the proxy's
    clock.
  - `payload` must be an object that the usage parser recognises.

Responses:

| Code | Meaning |
| --- | --- |
| 204 | Row written, or deduplicated against the last one |
| 400 | Content-Length isn't a number |
| 403 | Origin missing or not an extension |
| 405 | Method other than POST |
| 408 | Body took longer than 10 s to arrive |
| 411 | No Content-Length |
| 413 | Body over 256 KB |
| 415 | Content-Type isn't `application/json` |
| 422 | Envelope or payload rejected |

Rows land in the `claude_usage` table. See the main [README](../README.md#data).
