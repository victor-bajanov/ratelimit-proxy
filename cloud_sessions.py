#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Cloud Claude Code sessions: find one, dump its events, tally its plan credits.

Cloud sessions never pass through the proxy, yet they bill the same weekly
meter. Their usage is in the sessions API, which Claude Code itself calls:

  GET /v1/sessions?limit=100&after_id=X               session list, newest first
  GET /v1/sessions/{id}                               record; usage summed over models
  GET /v1/sessions/{id}/events?limit=100&after_id=X   event stream

A `result` event closes each turn and carries `model_usage_since_last_result`,
the per-model split the record's totals lack. Its cache writes and output
matched an exported transcript to within 0.1% (Nov 2026). `rate_limit_event`s
carry the 7d utilisation the session saw. A record's `environment_kind` is
`anthropic_cloud` for a cloud session and `bridge` for Remote Control, which
runs locally and so is already in the proxy's database.

  uv run cloud_sessions.py find "Session title"     print the id and record
  uv run cloud_sessions.py dump SESSION_ID [DIR]    save record.json + events.jsonl
  uv run cloud_sessions.py tally SESSION_ID|events.jsonl [--since ISO]
                                                    credits by model; defaults to
                                                    splitting at the current week
  uv run cloud_sessions.py week --since ISO [--split ISO ...] [--until ISO]
                                                    every cloud session AND routine
                                                    run in the window, by model, binned
                                                    at each split (e.g. a banked reset)

Routine runs never appear in GET /v1/sessions; they are listed per routine at
GET /v1/code/sessions?trigger_id=X (routines at /v1/code/triggers, which needs
the ccr-triggers beta).

Authenticates with Claude Code's own OAuth login from the macOS keychain. The
token goes to curl on stdin, so it is never in argv or in anything written out.
Every call is a read-only GET.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from proxy import credits

API = "https://api.anthropic.com"
# Sessions need ccr-byoc; routines (/v1/code/triggers) need ccr-triggers. Both
# together work for every endpoint here.
BETA = "ccr-byoc-2025-07-29,ccr-triggers-2026-01-30"
DUMP_DIR = os.path.expanduser("~/.claude/ratelimit-proxy/cloud-sessions")
MAX_PAGES = 1000
WEEK = timedelta(days=7)


# --------------------------------------------------------------------------- #
# reading the API

def make_get():
    """A GET function for the sessions API, authenticated as Claude Code.

    curl, not urllib: uv's Python does not trust the macOS keychain's roots."""
    cred = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                          capture_output=True, text=True, check=True).stdout
    headers = (f"Authorization: Bearer {json.loads(cred)['claudeAiOauth']['accessToken']}\n"
               f"anthropic-version: 2023-06-01\nanthropic-beta: {BETA}\n")

    def get(path: str):
        out = subprocess.run(["curl", "-s", "-H", "@-", "-w", "\n%{http_code}", API + path],
                             input=headers, capture_output=True, text=True).stdout
        body, _, code = out.rpartition("\n")
        try:
            return int(code), json.loads(body)
        except ValueError:
            return int(code or 0), body[:300]
    return get


def _pages(get, path: str):
    """Yield each page's rows, following after_id until has_more is false."""
    after = None
    for _ in range(MAX_PAGES):
        url = path + (f"&after_id={after}" if after else "")
        status, page = get(url)
        if status != 200 or not isinstance(page, dict):
            raise RuntimeError(f"GET {url} -> {status}: {str(page)[:200]}")
        yield page.get("data", [])
        if not page.get("has_more") or not page.get("last_id"):
            return
        after = page["last_id"]
    raise RuntimeError(f"GET {path}: more than {MAX_PAGES} pages")


def _cursor_pages(get, path: str):
    """Yield each page's rows, following next_cursor (the /v1/code listings)."""
    cur = None
    for _ in range(MAX_PAGES):
        url = path + (f"&cursor={cur}" if cur else "")
        status, page = get(url)
        if status != 200 or not isinstance(page, dict):
            raise RuntimeError(f"GET {url} -> {status}: {str(page)[:200]}")
        yield page.get("data", [])
        cur = page.get("next_cursor")
        if not cur:
            return
    raise RuntimeError(f"GET {path}: more than {MAX_PAGES} pages")


def list_triggers(get) -> list[dict]:
    """Every routine (trigger) on the account."""
    return [t for rows in _cursor_pages(get, "/v1/code/triggers?limit=100") for t in rows]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def routine_runs(get, since: datetime) -> list[dict]:
    """Routine run sessions active at or after `since`.

    They never appear in GET /v1/sessions, so a tally built from that listing
    alone misses them (55M credits in one half-week, Nov 2026). Every routine
    that has ever fired is checked, since a run can outlast its firing by days.
    Each run is {id: session_…, title, routine, created_at, last_event_at}."""
    out = []
    for t in list_triggers(get):
        if not t.get("last_fired_at"):
            continue
        for rows in _cursor_pages(get, f"/v1/code/sessions?trigger_id={t['id']}&limit=100"):
            for r in rows:
                if (r.get("last_event_at") or "") >= _iso(since):
                    out.append({"id": "session_" + r["id"].removeprefix("cse_"),
                                "title": r.get("title"), "routine": t.get("name"),
                                "created_at": r.get("created_at"),
                                "last_event_at": r.get("last_event_at")})
    return out


def cloud_sessions_since(get, since: datetime) -> list[dict]:
    """anthropic_cloud sessions updated at or after `since`. Bridge (Remote
    Control) sessions run locally, so the proxy already has them."""
    return [s for rows in _pages(get, "/v1/sessions?limit=100") for s in rows
            if s.get("environment_kind") == "anthropic_cloud"
            and (s.get("updated_at") or "") >= _iso(since)]


def find_session(get, title: str) -> dict | None:
    """The newest session whose title matches exactly, or None."""
    for rows in _pages(get, "/v1/sessions?limit=100"):
        for s in rows:
            if s.get("title") == title:
                return s
    return None


def fetch_events(get, sid: str) -> list[dict]:
    """Every event in a session, oldest first."""
    return [e for rows in _pages(get, f"/v1/sessions/{sid}/events?limit=100") for e in rows]


def fetch_record(get, sid: str) -> dict:
    status, rec = get(f"/v1/sessions/{sid}")
    if status != 200 or not isinstance(rec, dict):
        raise RuntimeError(f"GET /v1/sessions/{sid} -> {status}: {str(rec)[:200]}")
    return rec


# --------------------------------------------------------------------------- #
# tallying

def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class Turn:
    ts: datetime
    credits: dict[str, int]  # model -> plan credits for this turn


def turns(events: list[dict]) -> list[Turn]:
    """One Turn per `result` event, priced per model with the proxy's formula."""
    out = []
    for e in events:
        if e.get("type") != "result":
            continue
        priced = {}
        for model, mu in (e.get("model_usage_since_last_result") or {}).items():
            priced[model] = credits(model, mu.get("inputTokens", 0), mu.get("outputTokens", 0),
                                    mu.get("cacheCreationInputTokens", 0),
                                    mu.get("cacheReadInputTokens", 0))
        out.append(Turn(_ts(e["created_at"]), priced))
    return out


def tally(ts: list[Turn], since: datetime | None = None) -> dict:
    """Credits by model before and after `since` (all after when it is None)."""
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    for t in ts:
        side = before if since is not None and t.ts < since else after
        for model, c in t.credits.items():
            side[model] = side.get(model, 0) + c
    return {"before": before, "after": after,
            "total": sum(before.values()) + sum(after.values())}


def segment_tally(ts: list[Turn], cuts: list[datetime]) -> list[dict[str, int]]:
    """Credits by model in each span between sorted `cuts`: len(cuts) + 1 bins,
    the first before cuts[0] and the last from cuts[-1] on."""
    bins: list[dict[str, int]] = [{} for _ in range(len(cuts) + 1)]
    for t in ts:
        i = sum(t.ts >= c for c in cuts)
        for model, c in t.credits.items():
            bins[i][model] = bins[i].get(model, 0) + c
    return bins


def week_report(get, since: datetime, splits: list[datetime] = (),
                until: datetime | None = None, fetch=None) -> dict:
    """Credits by model for every off-machine session in [since, until), binned
    at `splits` (e.g. a banked reset): cloud sessions from /v1/sessions plus
    routine runs, which that listing omits. A session in both is listed once,
    as cloud. Turns outside [since, until) are dropped.

    -> {"sessions": [{id, title, kind, bins}], "totals": {kind: bins}}"""
    fetch = fetch or fetch_events
    found: dict[str, dict] = {}
    for s in cloud_sessions_since(get, since):
        found[s["id"]] = {"id": s["id"], "title": s.get("title"), "kind": "cloud"}
    for r in routine_runs(get, since):
        found.setdefault(r["id"], {"id": r["id"], "title": r["title"], "kind": "routine"})
    nbins = len(splits) + 1
    totals: dict[str, list[dict[str, int]]] = {}
    for s in found.values():
        ts = [t for t in turns(fetch(get, s["id"]))
              if t.ts >= since and (until is None or t.ts < until)]
        s["bins"] = segment_tally(ts, sorted(splits))
        kind = totals.setdefault(s["kind"], [{} for _ in range(nbins)])
        for i, b in enumerate(s["bins"]):
            for m, c in b.items():
                kind[i][m] = kind[i].get(m, 0) + c
    return {"sessions": list(found.values()), "totals": totals}


def meter_readings(events: list[dict]) -> list[tuple[datetime, dict[str, float]]]:
    """(time, {window: utilisation}) from each rate_limit_event."""
    out = []
    for e in events:
        if e.get("type") != "rate_limit_event":
            continue
        windows = (e.get("rate_limit_info") or {}).get("unifiedWindows") or {}
        out.append((_ts(e["created_at"]),
                    {k: v.get("utilization") for k, v in windows.items()}))
    return out


def week_start(next_reset: float, now: datetime) -> datetime:
    """Start of the week in force at `now`, given a reset time at or after it."""
    start = datetime.fromtimestamp(next_reset, timezone.utc)
    while start > now:
        start -= WEEK
    return start


# --------------------------------------------------------------------------- #
# command line

def _local(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %d %b %H:%M")


def _trim(rec: dict) -> dict:
    """The record without its bulky MCP tool list."""
    rec = json.loads(json.dumps(rec))
    ctx = rec.get("session_context") or {}
    if ctx.get("mcp_config"):
        ctx["mcp_config"] = f"<{len(json.dumps(ctx['mcp_config']))} bytes elided>"
    return rec


def cmd_find(args):
    get = make_get()
    s = find_session(get, args.title)
    if s is None:
        sys.exit(f"no session titled {args.title!r}")
    print("id:", s["id"])
    print(json.dumps(_trim(fetch_record(get, s["id"])), indent=2))


def cmd_dump(args):
    get = make_get()
    out = args.dir or os.path.join(DUMP_DIR, args.session)
    os.makedirs(out, exist_ok=True)
    rec = fetch_record(get, args.session)
    with open(os.path.join(out, "record.json"), "w") as f:
        json.dump(rec, f, indent=1)
    events = fetch_events(get, args.session)
    with open(os.path.join(out, "events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    print(f"{rec.get('title')!r} ({rec.get('environment_kind')}): {len(events)} events -> {out}")


def cmd_tally(args):
    rec = None
    if os.path.isfile(args.session):
        with open(args.session) as f:
            events = [json.loads(line) for line in f if line.strip()]
        rec_path = os.path.join(os.path.dirname(args.session), "record.json")
        if os.path.isfile(rec_path):
            with open(rec_path) as f:
                rec = json.load(f)
    else:
        get = make_get()
        rec = fetch_record(get, args.session)
        events = fetch_events(get, args.session)

    since = datetime.fromisoformat(args.since).astimezone(timezone.utc) if args.since else None
    reset = ((rec or {}).get("external_metadata") or {}).get("rate_limit_info", {}).get("resetsAt")
    if since is None and reset:
        since = week_start(reset, datetime.now(timezone.utc))

    if rec:
        print(f"{rec.get('title')!r}  {rec.get('environment_kind')}  {rec.get('configured_model')}"
              f"  {rec.get('created_at', '')[:16]} -> {rec.get('updated_at', '')[:16]} UTC")
    ts = turns(events)
    print("\nturns:")
    for t in ts:
        parts = "  ".join(f"{m.removeprefix('claude-')} {c / 1e6:.2f}M" for m, c in t.credits.items())
        print(f"  {_local(t.ts)}  {sum(t.credits.values()) / 1e6:5.2f}M  {parts}")
    print("\nmeter readings seen by the session:")
    for when, w in meter_readings(events):
        print(f"  {_local(when)}  " + "  ".join(f"{k}={v}" for k, v in w.items()))
    t = tally(ts, since)
    label = f"since {_local(since)}" if since else "all"
    print(f"\ncredits by model ({label}):")
    for side in ("before", "after") if since else ("after",):
        for model, c in sorted(t[side].items(), key=lambda kv: -kv[1]):
            print(f"  {side:6s} {model:22s} {c / 1e6:6.2f}M")
    print(f"  total  {t['total'] / 1e6:.2f}M, of which {sum(t['after'].values()) / 1e6:.2f}M {label}")


def _when(s: str) -> datetime:
    """An ISO time; naive means machine-local."""
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def cmd_week(args):
    since = _when(args.since)
    splits = sorted(_when(x) for x in args.split or [])
    until = _when(args.until) if args.until else None
    rep = week_report(make_get(), since, splits, until)
    edges = [since, *splits, until]
    labels = [f"{_local(a)} -> {_local(b) if b else 'now'}" for a, b in zip(edges, edges[1:])]

    def line(bins):
        return "  ".join(f"[{i}] {sum(b.values()) / 1e6:6.2f}M" for i, b in enumerate(bins))

    print("bins: " + "  ".join(f"[{i}] {l}" for i, l in enumerate(labels)))
    for s in sorted(rep["sessions"], key=lambda s: -sum(sum(b.values()) for b in s["bins"])):
        if any(s["bins"]):
            print(f"  {s['kind']:7s} {line(s['bins'])}  {str(s['title'])[:60]!r}  {s['id']}")
    print("\ntotals by kind and model:")
    for kind, bins in rep["totals"].items():
        print(f"  {kind:7s} {line(bins)}")
        for i, b in enumerate(bins):
            for m, c in sorted(b.items(), key=lambda kv: -kv[1]):
                print(f"          [{i}] {m:22s} {c / 1e6:6.2f}M")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find", help="find a session by exact title")
    f.add_argument("title")
    f.set_defaults(fn=cmd_find)
    d = sub.add_parser("dump", help="save a session's record and events")
    d.add_argument("session")
    d.add_argument("dir", nargs="?")
    d.set_defaults(fn=cmd_dump)
    t = sub.add_parser("tally", help="credits by model for a session")
    t.add_argument("session", help="session id, or a dumped events.jsonl")
    t.add_argument("--since", help="split point, ISO time (default: start of the current week)")
    t.set_defaults(fn=cmd_tally)
    w = sub.add_parser("week", help="every cloud session and routine run in a window")
    w.add_argument("--since", required=True, help="window start, ISO (naive = local)")
    w.add_argument("--split", action="append", help="bin boundary, ISO; repeatable")
    w.add_argument("--until", help="window end, ISO (default: now)")
    w.set_defaults(fn=cmd_week)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
