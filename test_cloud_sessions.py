#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8"]
# ///
"""Tests for cloud_sessions.py.  Run with:  uv run test_cloud_sessions.py"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

import cloud_sessions as cs
import proxy


class FakeAPI:
    """Pages of rows keyed by the after_id that asks for them; records each path."""
    def __init__(self, base, pages):
        self.base, self.pages, self.paths = base, pages, []

    def __call__(self, path):
        self.paths.append(path)
        after = path.split("after_id=")[1] if "after_id=" in path else None
        rows, more = self.pages[after]
        return 200, {"data": rows, "has_more": more,
                     "first_id": rows[0]["id"], "last_id": rows[-1]["id"]}


def sessions_api():
    return FakeAPI("/v1/sessions", {
        None: ([{"id": "s1", "title": "Alpha"}, {"id": "s2", "title": "Beta"}], True),
        "s2": ([{"id": "s3", "title": "Gamma"}], False)})


def events_api():
    return FakeAPI("/v1/sessions/X/events", {
        None: ([{"id": "a"}, {"id": "b"}], True),
        "b": ([{"id": "c"}, {"id": "d"}], True),
        "d": ([{"id": "e"}], False)})


# --- reading the API ----------------------------------------------------------

def test_importing_touches_neither_keychain_nor_network(monkeypatch):
    import importlib, subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ran a subprocess"))
    importlib.reload(cs)


def test_find_session_pages_by_after_id_until_the_title_matches():
    api = sessions_api()
    assert cs.find_session(api, "Gamma")["id"] == "s3"
    assert api.paths == ["/v1/sessions?limit=100", "/v1/sessions?limit=100&after_id=s2"]


def test_find_session_stops_at_the_first_match():
    api = sessions_api()
    assert cs.find_session(api, "Alpha")["id"] == "s1"
    assert len(api.paths) == 1


def test_find_session_returns_none_when_no_title_matches():
    assert cs.find_session(sessions_api(), "Nope") is None


def test_an_error_status_raises_rather_than_passing_silently():
    with pytest.raises(RuntimeError):
        cs.find_session(lambda p: (500, "boom"), "Alpha")
    with pytest.raises(RuntimeError):
        cs.fetch_events(lambda p: (404, "404 page not found"), "X")


def test_fetch_events_reads_every_page_by_after_id_and_nothing_else():
    api = events_api()
    assert [e["id"] for e in cs.fetch_events(api, "X")] == ["a", "b", "c", "d", "e"]
    assert api.paths == ["/v1/sessions/X/events?limit=100",
                         "/v1/sessions/X/events?limit=100&after_id=b",
                         "/v1/sessions/X/events?limit=100&after_id=d"]


# --- tallying a session's usage -------------------------------------------------

def result(ts, usage):
    """A `result` event: one turn, with usage since the previous result per model."""
    return {"type": "result", "created_at": ts, "model_usage_since_last_result": {
        m: {"inputTokens": i, "cacheCreationInputTokens": cw,
            "cacheReadInputTokens": cr, "outputTokens": o}
        for m, (i, cw, cr, o) in usage.items()}}


EVENTS = [
    {"type": "assistant", "created_at": "2026-11-17T13:00:00Z"},
    result("2026-11-17T13:30:00Z", {"claude-fable-5-2": (10, 1_000_000, 5_000_000, 100_000),
                                    "claude-haiku-5-5": (200_000, 0, 0, 10_000)}),
    {"type": "rate_limit_event", "created_at": "2026-11-17T21:37:00Z",
     "rate_limit_info": {"unifiedWindows": {"seven_day": {"utilization": 0.01}}}},
    result("2026-11-17T21:38:00Z", {"claude-fable-5-2": (5, 500_000, 9_000_000, 50_000)}),
    result("2026-11-19T00:44:00Z", {"claude-opus-5-5": (0, 300_000, 0, 20_000)}),
]


def test_turns_price_each_model_with_the_proxys_credit_formula():
    turns = cs.turns(EVENTS)
    assert len(turns) == 3
    first = turns[0]
    assert first.ts == datetime(2026, 11, 17, 13, 30, tzinfo=timezone.utc)
    assert first.credits == {
        "claude-fable-5-2": proxy.credits("claude-fable-5-2", 10, 100_000, 1_000_000, 5_000_000),
        "claude-haiku-5-5": proxy.credits("claude-haiku-5-5", 200_000, 10_000, 0, 0)}


def test_tally_splits_credits_by_model_either_side_of_since():
    since = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)
    t = cs.tally(cs.turns(EVENTS), since=since)
    fable = proxy.credits("claude-fable-5-2", 5, 50_000, 500_000, 9_000_000)
    opus = proxy.credits("claude-opus-5-5", 0, 20_000, 300_000, 0)
    assert t["after"] == {"claude-fable-5-2": fable, "claude-opus-5-5": opus}
    assert set(t["before"]) == {"claude-fable-5-2", "claude-haiku-5-5"}
    assert t["total"] == sum(t["before"].values()) + sum(t["after"].values())


def test_tally_without_since_puts_everything_after():
    t = cs.tally(cs.turns(EVENTS))
    assert t["before"] == {}
    assert sum(t["after"].values()) == t["total"]


def test_meter_readings_come_from_rate_limit_events():
    assert cs.meter_readings(EVENTS) == [
        (datetime(2026, 11, 17, 21, 37, tzinfo=timezone.utc), {"seven_day": 0.01})]


def test_last_week_start_is_the_most_recent_reset_before_now():
    now = datetime(2026, 11, 19, 10, tzinfo=timezone.utc)
    reset = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)  # next reset, as the API reports it
    assert cs.week_start(reset.timestamp(), now) == datetime(2026, 11, 17, 15, tzinfo=timezone.utc)


# --- routine runs and the week's cloud usage -----------------------------------

class CursorAPI:
    """Cursor-paged listings: {path: [page rows, ...]}; page n+1 is asked for with
    cursor=c<n>. Records each path."""
    def __init__(self, listings):
        self.listings, self.paths = listings, []

    def __call__(self, path):
        self.paths.append(path)
        base, _, cur = path.partition("&cursor=")
        pages = self.listings[base]
        n = int(cur[1:]) if cur else 0
        more = n + 1 < len(pages)
        return 200, {"data": pages[n], "has_more": more,
                     "next_cursor": f"c{n + 1}" if more else None}


def test_the_api_asks_for_the_triggers_beta_as_well_as_sessions():
    assert "ccr-byoc-2025-07-29" in cs.BETA
    assert "ccr-triggers-2026-01-30" in cs.BETA


def test_list_triggers_follows_next_cursor():
    api = CursorAPI({"/v1/code/triggers?limit=100": [[{"id": "t1"}], [{"id": "t2"}]]})
    assert [t["id"] for t in cs.list_triggers(api)] == ["t1", "t2"]
    assert api.paths == ["/v1/code/triggers?limit=100", "/v1/code/triggers?limit=100&cursor=c1"]


def test_routine_runs_keeps_runs_active_since_and_names_them_by_session_id():
    since = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)
    api = CursorAPI({
        "/v1/code/triggers?limit=100": [[
            {"id": "t1", "name": "Daily scan", "last_fired_at": "2026-11-24T22:03:00Z"},
            {"id": "t2", "name": "Never fired", "last_fired_at": None},
            # fired before the window, but its run kept going into it
            {"id": "t3", "name": "Long run", "last_fired_at": "2026-11-16T01:00:00Z"}]],
        "/v1/code/sessions?trigger_id=t1&limit=100": [
            [{"id": "cse_A", "title": "scan 24", "last_event_at": "2026-11-24T22:08:00Z"}],
            [{"id": "cse_B", "title": "scan 10", "last_event_at": "2026-11-10T22:08:00Z"}]],
        "/v1/code/sessions?trigger_id=t3&limit=100": [
            [{"id": "cse_C", "title": "long", "last_event_at": "2026-11-18T03:00:00Z"}]],
    })
    runs = cs.routine_runs(api, since)
    assert [(r["id"], r["title"], r["routine"]) for r in runs] == [
        ("session_A", "scan 24", "Daily scan"), ("session_C", "long", "Long run")]
    assert not any("trigger_id=t2" in p for p in api.paths)


def test_cloud_sessions_since_keeps_only_cloud_sessions_updated_since():
    since = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)
    api = FakeAPI("/v1/sessions", {None: ([
        {"id": "s1", "environment_kind": "anthropic_cloud", "updated_at": "2026-11-24T13:21:00Z"},
        {"id": "s2", "environment_kind": "bridge", "updated_at": "2026-11-24T13:21:00Z"},
        {"id": "s3", "environment_kind": "anthropic_cloud", "updated_at": "2026-11-11T11:00:00Z"},
    ], False)})
    assert [s["id"] for s in cs.cloud_sessions_since(api, since)] == ["s1"]


def test_segment_tally_bins_each_turn_between_the_cuts():
    cuts = [datetime(2026, 11, 17, 15, tzinfo=timezone.utc),
            datetime(2026, 11, 19, tzinfo=timezone.utc)]
    t = cs.segment_tally(cs.turns(EVENTS), cuts)
    fable1 = proxy.credits("claude-fable-5-2", 10, 100_000, 1_000_000, 5_000_000)
    fable2 = proxy.credits("claude-fable-5-2", 5, 50_000, 500_000, 9_000_000)
    assert t[0]["claude-fable-5-2"] == fable1           # before the first cut
    assert t[1] == {"claude-fable-5-2": fable2}         # between the cuts
    assert set(t[2]) == {"claude-opus-5-5"}             # after the last cut
    assert len(t) == 3


def test_week_report_covers_cloud_sessions_and_routine_runs_binned_by_split():
    since = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)
    split = datetime(2026, 11, 19, tzinfo=timezone.utc)
    until = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)
    sessions = FakeAPI("/v1/sessions", {None: ([
        {"id": "session_A", "title": "Approval", "environment_kind": "anthropic_cloud",
         "updated_at": "2026-11-24T13:00:00Z"}], False)})
    cursor = CursorAPI({
        "/v1/code/triggers?limit=100": [[{"id": "t1", "name": "orch", "last_fired_at": "2026-11-23T01:00:00Z"}]],
        "/v1/code/sessions?trigger_id=t1&limit=100": [[
            {"id": "cse_R", "title": "orch run", "last_event_at": "2026-11-24T04:00:00Z"},
            {"id": "cse_A", "title": "Approval", "last_event_at": "2026-11-24T13:00:00Z"}]]})

    def get(path):
        return (sessions if path.startswith("/v1/sessions") else cursor)(path)

    events = {
        "session_A": EVENTS,
        "session_R": [result("2026-11-23T02:00:00Z", {"claude-opus-5-5": (0, 100_000, 0, 1_000)}),
                      result("2026-11-25T02:00:00Z", {"claude-opus-5-5": (0, 900_000, 0, 9_000)})],
    }
    rep = cs.week_report(get, since, [split], until, fetch=lambda g, sid: events[sid])

    assert [(s["id"], s["kind"]) for s in rep["sessions"]] == [
        ("session_A", "cloud"), ("session_R", "routine")]   # Approval listed once
    a, r = rep["sessions"]
    fable2 = proxy.credits("claude-fable-5-2", 5, 50_000, 500_000, 9_000_000)
    opus_a = proxy.credits("claude-opus-5-5", 0, 20_000, 300_000, 0)
    assert a["bins"] == [{"claude-fable-5-2": fable2}, {"claude-opus-5-5": opus_a}]  # 13:30 turn dropped
    assert r["bins"] == [{}, {"claude-opus-5-5": proxy.credits("claude-opus-5-5", 0, 1_000, 100_000, 0)}]
    assert rep["totals"]["routine"][1] == r["bins"][1]
    assert rep["totals"]["cloud"][0] == {"claude-fable-5-2": fable2}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
