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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
