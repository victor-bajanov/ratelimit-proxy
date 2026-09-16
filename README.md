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
uv run proxy.py cert                # renew the leaf (automatic at startup too)
uv run proxy.py serve               # foreground, for debugging
uv run proxy.py uninstall
```

`status` prints something like:

```
launchd : running  pid 80533
health  : ok, up 2:14:09, 812 requests, 0 errors, 96 rows written, 0 dropped
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

Both are plain SQLite, so ad-hoc questions don't need this tool:

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
have processed a request it never received.

## Provenance

Grew out of a throwaway script in a Claude Code scratchpad
(`ratelimit_proxy.py`, Sept 2026) written alongside a few other scripts while
working out how Max 20x plan limits actually behave. Those answered "what did
I spend"; this answers "what does Anthropic think I spent", which turned out
to be the more useful question.
