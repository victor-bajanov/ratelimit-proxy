# claude-ratelimit-proxy

A localhost proxy that sits in front of `api.anthropic.com` and records the
`anthropic-ratelimit-unified-*` response headers to SQLite.

Those headers are the authoritative plan-utilisation signal — Anthropic's own
view of where you sit against the 5h and 7d windows. Everything else (parsing
`~/.claude/projects/**/*.jsonl`, `ccusage`, credit arithmetic) is a
reconstruction. This captures the source, continuously, so you build a real
history instead of a snapshot.

It also sniffs per-request model and token counts out of the response stream,
which gives you the same numbers a transcript parser would derive, without
the transcript-parsing caveats (no duplicate `message.id` rows, no
`<synthetic>` entries, no missing requests).

## How it gets into the path

By impersonating `api.anthropic.com`, not by being pointed at.

`ANTHROPIC_BASE_URL` was the obvious lever and it is the wrong one: since
v2.1.196 Claude Code refuses Remote Control whenever that variable names a host
other than `api.anthropic.com`, so pairing with claude.ai or the phone app dies
the moment you route through a proxy. The documented escape hatch
(`_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL`) explicitly does not apply to
Remote Control.

So the base URL stays untouched and the name moves instead:

* `/etc/hosts` sends `api.anthropic.com` to `127.0.0.1`
* the proxy answers on `:443` with a leaf certificate for that name, signed by
  a local CA in the System keychain
* the proxy finds the real upstream by querying the nameservers in
  `/etc/resolv.conf` over UDP itself, because the system resolver would now
  send it back to itself

Claude Code sees a default configuration talking to a valid `api.anthropic.com`
over TLS, and Remote Control works.

Binding `:443` needs no root. macOS only demands privilege to bind a low port
to a *specific* address; the wildcard bind is exempt. The cost is that the
socket is reachable from the network, so non-loopback peers are dropped on the
accept path, before the handshake — a port scan sees a closed connection and
never gets offered a certificate.

## Install

```
uv run proxy.py cert                # create the CA + leaf
uv run proxy.py install             # launchd agent, starts listening on :443
uv run proxy.py trust               # prints the two steps that need sudo
```

`install` writes `~/Library/LaunchAgents/id.bajanov.claude-ratelimit-proxy.plist`.
`RunAtLoad` + `KeepAlive` mean it comes back after login and after a crash.

The remaining steps need root, so `trust` prints them rather than running them.
**Order matters, and the middle step is the one people skip:**

```
# 1. trust the CA
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain ~/.claude/ratelimit-proxy/tls/ca.pem

# 2. restart every client that predates the CA — see below
#    `uv run proxy.py status` lists them on a `stale:` line

# 3. only now redirect the name
printf '\n# claude-ratelimit-proxy\n127.0.0.1 api.anthropic.com\n::1 api.anthropic.com\n' \
  | sudo tee -a /etc/hosts
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

A process reads its trust store once, at startup. Trusting a new root does
nothing for anything already running: Claude Desktop that has been up for a
day, an open Claude Code session, a long-lived Chrome. Redirect the name while
those are running and every one of them fails TLS verification until it
restarts — which reads as the intercept being broken when it is working
exactly as designed. Hence step 2, and hence the `stale:` line in `status`.

Then remove `ANTHROPIC_BASE_URL` from `~/.claude/settings.json` and any shell
profile. Leaving it set defeats the entire exercise.

## Use

```
uv run proxy.py status              # launchd state, health, intercept, latest signal
uv run proxy.py history --days 7    # utilisation over time
uv run proxy.py usage --days 7      # tokens + credits by model
uv run proxy.py prompts             # request bodies captured, if config.json asks
uv run proxy.py cert                # renew the leaf (automatic at startup too)
uv run proxy.py serve               # foreground, for debugging
uv run proxy.py uninstall
```

`status` prints something like:

```
launchd : running  pid 80533
health  : ok, up 2:14:09, 812 requests, 0 errors, 96 rows written, 0 dropped
pool    : 0 dead pooled conns dropped, 0 stale-timeout recoveries
fds     : 41 of 10240 open
hosts   : api.anthropic.com -> 127.0.0.1, ::1
cert    : leaf expires Fri 15 Oct 2027 (396d), CA trusted
tls     : listening on :443
db      : ~/.claude/ratelimit-proxy/ratelimit.db  (96 signal rows, 812 requests)

latest signal (Sat 12 Sep 13:58), status allowed_warning:
  5h         1.0%  [........................................]  resets Sat 12 Sep 18:50
  7d        77.0%  [###############################.........]  resets Wed 16 Sep 01:00
```

## Operational time: what does a token cost?

```
uv run optime.py                    # every meter: dataset summary + fit
uv run optime.py --meter 5h --show  # print the per-tick interval table
uv run optime.py --csv ticks.csv    # dump the dataset for your own analysis
uv run optime.py --by-model         # one coefficient per (model, counter)
```

The meters are rounded to 1%, so calendar time is the wrong clock for
relating tokens to utilisation. `optime.py` re-indexes the data on
*operational time* -- one unit is one tick of the meter -- the way a claims
development triangle is indexed on development period rather than the
calendar. For each meter (5h, 7d, 7d-opus) it finds the boundary request that
first carried each new level, aggregates every token counter between
consecutive boundaries, and fits

```
ticks = b_in * input + b_out * output + b_cw * cache_write + b_cr * cache_read
```

with no intercept. Censored intervals are dropped: the first level seen in
each window (start of the data, and the start of every window after a reset),
the tail after the last boundary (end of data, and the run-up to a reset), and
intervals with no recorded traffic (the tick was earned on another machine).
Within a window the meter can only rise, so the first sighting of each new
running-max level is the boundary; stale lower values from long-running
concurrent responses are ignored.

The script reads whatever the database holds and refits on all of it, so
rerun it as the history grows. The proxy sees one machine while the meter
counts the whole account, so absolute coefficients are biased upwards and the
implied window cap is a floor; the ratios between counters are the robust
output. A single-coefficient fit on `credits()` is printed alongside as a
sanity check against the pricing table.

## What else this catches

A hosts entry is machine-wide, so this is no longer just Claude Code's proxy.
Every process on the machine that talks to `api.anthropic.com` now goes through
it: the Claude desktop app, the Chrome extension, `curl`, MCP servers. Usually
that is a feature — the utilisation picture gets more complete, not less.

It does mean the CA has to be trusted machine-wide rather than handed to one
process, and clients carrying their own trust store won't honour it:

* **Python** (`requests`, `httpx`) uses `certifi` — set `SSL_CERT_FILE` or
  `REQUESTS_CA_BUNDLE` to the CA for those.
* **Firefox** has its own store and needs the CA imported separately.
* Node and Bun read the OS store, so Claude Code itself is fine.

The leaf is valid 397 days because Apple enforces a 398-day ceiling on TLS
server certificates even under a locally added root. `serve` reissues it
automatically once it is within 30 days of expiry; the CA is untouched, so the
keychain trust survives.

## claude.ai usage by surface

The API headers give one 7d number for the whole account. claude.ai's usage
page also splits the week by surface (Claude Code, Chats, Cowork, Other) and
shows the model-scoped weekly meters. The Chrome extension in
[`usage-extension/`](usage-extension/README.md) picks that up. Whenever a
claude.ai page fetches its usage JSON, the extension re-fetches it with the
browser's cookies and POSTs it to `POST /__proxy/claude-usage` on the plain
listener. The proxy never relays claude.ai traffic; this endpoint only receives
what the extension sends.

The endpoint only takes JSON from a `chrome-extension://` origin, up to 256 KB,
and sends no CORS headers, so an ordinary web page can't write to it. It
accepts *any* extension id, so another installed extension with access to
127.0.0.1 could plant false rows. It couldn't read anything, and nothing is
relayed; pinning the id is left for later. The TLS listener doesn't serve the
endpoint at all. The extension has port 8787 hard-coded, so a proxy on another
`--port` needs the extension changed too. `status` gains two lines once a row exists:

```
latest claude.ai usage (Wed 23 Sep 15:40): 5h …%  7d …%  7d <model> …%
  this week by surface: Claude Code 79%, Chats 2%, Cowork 19%, Other 0%
```

## Capturing prompts

To find out what a request actually *is*, say a background call nobody can
explain, the proxy can keep the full request body for chosen models. It is off
by default. Turn it on in `~/.claude/ratelimit-proxy/config.json` (next to the
database; `CLAUDE_RATELIMIT_DIR` moves both):

```json
{"log_prompts": {"models": ["claude-sonnet-5"], "max_bytes": 8000000}}
```

* `models` holds exact model names or globs (`"claude-haiku-*"`, `"*"` for
  everything). Globs are Python's `fnmatch`, so `[` opens a character class;
  a pattern that equals the model name exactly always matches, so
  `"claude-sonnet-5[1m]"` still means that name. The model is the request
  body's top-level `model` field, i.e. the name the client asked for
  (`claude-sonnet-5[1m]` is not `claude-sonnet-5`). Only the first 64 KiB of a
  body is read to find it, so a big body is never parsed in full on the
  proxy's time; Claude Code sends `model` first. A body that isn't JSON, or
  has no `model` in that head, is never kept. An empty list, no `log_prompts`,
  or no file at all means off.
* `max_bytes` (default 8,000,000) caps each stored body before compression. A
  longer one is kept up to the cap and flagged `truncated`; upstream still
  gets all of it.
* Other keys are ignored, so the file can grow.

**No restart needed.** The running proxy re-checks the file at most every two
seconds and re-reads it when it changes; `kill -HUP <pid>` reloads at once. A
file that won't parse, or has a wrong-typed value, is not applied: the last good
config stays in force (so a typo never turns logging on), the error goes to
the launchd log once, and `status` shows it until the file is fixed. Deleting
the file turns logging off.

`status` says when capture is on:

```
prompts : logging claude-sonnet-5 (14 captured)
```

and `prompts` reads them back:

```
uv run proxy.py prompts                          # the latest 50, joined to requests
uv run proxy.py prompts --model 'claude-son*' --limit 10
uv run proxy.py prompts --request-id req_011C…   # settings, system prompt, last 3 messages
uv run proxy.py prompts --request-id req_011C… --full | jq .   # the whole body
```

`--model` takes an exact name or the same kind of glob (SQLite `GLOB`). The
list shows each request's output tokens and stop reason when its
`requests` row exists. Use `--id N` for a row with no request id.

**Privacy.** A captured body is everything that was sent to the model: the
system prompt, the whole conversation, file contents, tool output. It is
stored only in the local database and never leaves the machine, but it stays
there until you delete it (`DELETE FROM prompts`). Request headers, which carry
the credentials, are never stored. Turn capture on for the narrowest model
list that answers the question, and off again afterwards.

## Data

`~/.claude/ratelimit-proxy/ratelimit.db`, WAL mode, safe to read while the
proxy is running.

**`ratelimit`** — one row per *change* in the signal, not per request. A busy
hour where utilisation doesn't move produces one row, so the table stays small
and every row is a real event. Each row carries the `request_id` of the
response that delivered it, so a signal change can be joined back to the
request (and its start time) that observed it. Known headers get typed
columns; anything new
that matches `ratelimit|quota|retry-after` is preserved verbatim in `extra` as
JSON, so a header Anthropic starts sending tomorrow is captured today.

**`requests`** — one row per API request: model, input/output/cache tokens,
stop reason, duration. Deduped on `request_id`.

**`claude_usage`** — one row per *change* in the claude.ai usage page, as sent
by the extension: org, 5h and 7d utilisation and resets (fractions, like
`ratelimit`, though the page only reports whole percents), the model-scoped
weekly meters in `scoped`, `seven_day_breakdown` verbatim in `breakdown`, and
the whole payload in `raw` so a shape change loses nothing.

**`prompts`** — only when `config.json` asks for it (see above): one row per
captured request body, with `request_id` to join `requests`, `path`, `model`,
`bytes` (the size sent), `truncated`, and the body itself as a BLOB compressed
per `encoding` (`zlib`; `python -c 'import zlib,sys;
sys.stdout.buffer.write(zlib.decompress(sys.stdin.buffer.read()))'` undoes it).
A body is kept even when upstream sent no request id; `request_id` is then NULL.

All of them are plain SQLite, so ad-hoc questions don't need this tool:

```sql
-- when did the 7d window last cross 80%?
SELECT datetime(ts,'unixepoch','localtime'), seven_d_utilization
FROM ratelimit WHERE seven_d_utilization >= 0.8 ORDER BY ts LIMIT 1;
```

## Design notes

**Fail open.** This process is in the path of every Claude Code request, so a
logging bug must never cost a session. Writes go to a bounded queue drained by
a background thread; a full queue or a broken database drops samples rather
than blocking or raising. Usage sniffing is wrapped and best-effort — the
stream is relayed first, parsed second.

**If it ever misbehaves**, delete the hosts entry:

```
sudo sed -i '' '/api.anthropic.com/d' /etc/hosts
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

The name resolves normally again and every client goes straight to Anthropic.
Nothing else has to be undone — the CA sitting untrusted in the keychain and a
listener on `:443` are both inert once the name stops pointing at them.

Recovery got sharper than it was, and the blast radius got wider with it. Under
`ANTHROPIC_BASE_URL` a dead proxy killed Claude Code; under a hosts entry it
kills every `api.anthropic.com` client on the machine. `KeepAlive` covers
crashes. It does not cover deleting the plist and leaving `/etc/hosts` set, and
it does not cover a leaf certificate you let expire on a machine that never
restarts the agent — hence the renew-on-startup and the expiry in `status`.

The plain-HTTP listener on `127.0.0.1:8787` stays up as a second way back in:
if TLS or the certificate is the thing that's broken, `ANTHROPIC_BASE_URL`
still works while you sort it out.

**Credentials** are forwarded and never written. The `SECRET` pattern excludes
anything matching `authorization|api-key|cookie|token|secret` from the record
path, and request bodies are never touched. Only loopback peers are served:
that check moved from a `127.0.0.1` bind to an explicit peer test when the
listener went to a wildcard `:443`, and it runs before the TLS handshake. An
open proxy relaying your OAuth token would be a genuinely bad day.

**The CA key** at `~/.claude/ratelimit-proxy/tls/ca.key` (mode `600`, in a
`700` directory) signs certificates your machine trusts for any name it is
asked to. Anyone who reads it can impersonate any site to you. It never leaves
the machine and is generated locally, but it is the most sensitive file this
tool creates.

**`accept-encoding` is stripped** from forwarded requests so upstream replies
in identity and the token counts stay readable without a gunzip step. Costs a
little bandwidth on responses; they're small.

**Connection pooling** — a LIFO pool of TLS connections with a 50s idle TTL,
because a fresh handshake per turn is a tax you'd feel. A connection error
before any response byte is retried once on a fresh connection; upstream can't
have processed a request it never received. Connect and handshake get their
own 15s budget; the long read timeout exists for first-byte latency, not for
a SYN nobody answers.

**Streaming relays chunk by chunk.** The relay reads upstream with `read1`,
not `read`: on a chunked body `read(n)` keeps pulling until it has `n` bytes
or the stream ends, which turned every SSE response into a spinner followed
by a 64K burst. Upstream failures are logged with a timestamp, always — each
one is a 502 the client backs off and retries, which reads as a pause.

**File descriptors.** launchd starts the process with a soft limit of 256.
Every idle keep-alive client and every pooled upstream connection holds one,
and at the ceiling `accept()` fails and every `api.anthropic.com` client on
the machine hangs. `serve` raises the soft limit to 10240 at startup — forty
times anything observed, and low enough that a real leak crashes this process
(launchd restarts it) rather than crowding the system table.

## Provenance

Grew out of a throwaway script in a Claude Code scratchpad
(`ratelimit_proxy.py`, Sept 2026) written alongside a few other scripts while
working out how Max 20x plan limits actually behave. Those answered "what did
I spend"; this answers "what does Anthropic think I spent", which turned out
to be the more useful question.
