#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "numpy>=1.26"]
# ///
"""Tests for optime.py.  Run with:  uv run test_optime.py"""
from __future__ import annotations

import sys

import numpy as np
import pytest

import optime
import proxy


def test_list_price_keeps_opus_5_5_at_list_while_credits_use_the_plan_rate():
    assert proxy.LIST_PRICE["claude-opus-5-5"] == (4, 20)
    assert proxy.PRICE["claude-opus-5-5"] == (2.32, 11.6)
    assert proxy.LIST_PRICE.keys() == proxy.PRICE.keys()


def test_list_dollars_scales_each_counter_by_the_models_list_price():
    # input, cache writes and cache reads at the input price; output at output.
    assert optime.list_dollars("claude-opus-5-5", 1_000_000, 1_000_000,
                               1_000_000, 1_000_000) == (4, 20, 4, 4)
    assert optime.list_dollars("claude-fable-5-1", 500_000, 100_000, 0, 0) == (5, 5, 0, 0)


def test_list_dollars_applies_the_long_prompt_tier():
    # Haiku 5.5: prompt over 100K (cache reads included) is $0.5/$2.5.
    assert optime.list_dollars("claude-haiku-5-5", 50_000, 1_000_000, 0, 0) == \
        pytest.approx((0.005, 0.5, 0, 0))
    assert optime.list_dollars("claude-haiku-5-5", 50_000, 1_000_000, 0, 60_000) == \
        pytest.approx((0.025, 2.5, 0, 0.03))


def test_list_dollars_prices_unknown_models_at_nothing():
    assert optime.list_dollars("claude-mystery", 1_000, 1_000, 1_000, 1_000) == (0, 0, 0, 0)


def _db(tmp_path, requests, signal=()):
    path = tmp_path / "t.db"
    conn = proxy.connect(path)
    conn.executemany(
        "INSERT INTO requests (ts, model, input_tokens, output_tokens, "
        "cache_creation_tokens, cache_read_tokens) VALUES (?,?,?,?,?,?)", requests)
    conn.executemany(
        "INSERT INTO ratelimit (ts, five_h_utilization, five_h_reset) VALUES (?,?,?)",
        signal)
    conn.commit()
    conn.close()
    return path


def test_fill_accumulates_list_dollars_per_counter(tmp_path):
    path = _db(tmp_path, [
        (150, "claude-opus-5-5", 1_000_000, 0, 0, 0),
        (160, "claude-fable-5-1", 0, 100_000, 200_000, 1_000_000),
        (170, "claude-mystery", 9_000_000, 0, 0, 0),
    ])
    with proxy.connect(path, readonly=True) as conn:
        requests = optime.Requests(conn)
    iv = optime.Interval("5h", 1, 100, 200, 1, 2)
    requests.fill(iv)
    assert iv.dollars == pytest.approx([4, 5, 2, 10])
    assert iv.list_total("output") == pytest.approx(5)


def _iv(dollars):
    iv = optime.Interval("5h", 1, 0, 1, 0, 0)
    iv.dollars = list(dollars)
    return iv


def test_priced_fit_recovers_ticks_per_list_dollar():
    rng = np.random.default_rng(0)
    truth = {"input": 0.5, "output": 0.2, "cache_write": 0.4, "cache_read": 0.02}
    intervals = []
    for _ in range(40):
        d = rng.uniform(1, 20, 4)
        iv = _iv(d)
        iv.lvl1 = float(sum(b * x for b, x in zip(truth.values(), d)))
        intervals.append(iv)
    data = optime.MeterData("5h", intervals=intervals)
    fit = optime.fit_meter_priced(data, list(optime.COUNTERS))
    assert fit.names == list(optime.COUNTERS)
    assert fit.beta == pytest.approx(list(truth.values()))


def test_priced_fit_uses_only_the_chosen_counters():
    intervals = [_iv([k, 2 * k, 100, 100]) for k in range(1, 6)]
    for iv in intervals:
        iv.lvl1 = iv.dollars[0] * 0.3 + iv.dollars[1] * 0.1
    fit = optime.fit_meter_priced(optime.MeterData("5h", intervals=intervals),
                                  ["input", "output"])
    assert fit.names == ["input", "output"]


def test_report_priced_fit_prints_ticks_and_dollars_per_tick(capsys):
    intervals = [_iv([k, 3 * k + 1, 0, 0]) for k in range(1, 6)]
    for iv in intervals:
        iv.lvl1 = iv.dollars[0] * 0.5 + iv.dollars[1] * 0.25
    data = optime.MeterData("5h", intervals=intervals)
    fit = optime.fit_meter_priced(data, ["input", "output"])
    optime.report_priced_fit(fit, data)
    out = capsys.readouterr().out
    assert "ticks/$" in out and "$/tick" in out
    assert "2.00" in out   # $ per tick on input
    assert "4.00" in out   # $ per tick on output
    assert "0.50" in out   # output relative to input


def test_main_prints_a_list_priced_fit(tmp_path, capsys):
    # One 5h window, one tick per 100 s, Opus traffic only: the priced fit
    # is the token fit divided by Opus's list price.
    signal = [(t, k / 100, 9999) for k, t in enumerate(range(100, 1200, 100), 1)]
    rng = np.random.default_rng(1)
    reqs = [(t + 50, "claude-opus-5-5", int(rng.integers(1, 9) * 100_000),
             int(rng.integers(1, 9) * 10_000), 0, 0) for t, _, _ in signal]
    path = _db(tmp_path, reqs, signal)
    assert optime.main(["--db", str(path), "--meter", "5h",
                        "--counters", "input,output"]) == 0
    out = capsys.readouterr().out
    assert "list-priced token fit" in out
    assert "ticks/$" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
