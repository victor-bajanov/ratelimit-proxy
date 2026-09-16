#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26"]
# ///
"""Operational-time analysis of the plan meters.

The `anthropic-ratelimit-unified-*` headers report utilisation rounded to 1%,
so calendar time is the wrong clock for asking "what does a token cost". The
right clock is *operational time*, as in claims-development analysis: one unit
of time is one tick of the meter, and everything that happened between two
ticks is one observation.

For each meter (5h, 7d, 7d-opus) the algorithm is:

  1. Take every signal row carrying that meter, ordered by time, and split
     into windows on the meter's reset timestamp.
  2. Inside a window the meter can only rise, so the *first* sighting of each
     new running-max level is a tick boundary. Later rows showing a lower
     value are stale headers from long-running concurrent responses (the
     header is generated at response start, the row is written at response
     end) and are ignored. A jump of more than one level is one boundary
     with a delta of several ticks. Where the signal row names the request
     that carried it, the boundary is placed at that request's *start*
     (completion minus duration), which is when the header was generated;
     older rows without a request id fall back to completion time.
  3. Consecutive boundaries bracket an interval. Every priced request whose
     completion falls in (t_from, t_to] is aggregated into it, per model and
     per token counter.
  4. Censored intervals are dropped: the first level seen in each window (we
     did not observe the tick into it, so we do not know where it started --
     this covers the start of the data and the start of every window), and
     the tail after the last boundary (end of data, or the run-up to a reset).
     Intervals with no recorded request are dropped too; the tick was earned
     on a machine this proxy does not see.

The result is a dataset with one row per interval: ticks earned, and the
token counters accumulated while earning them. A no-intercept least-squares
fit of

    ticks = b_in * input + b_out * output + b_cw * cache_write + b_cr * cache_read

then estimates what each counter costs in meter units. The proxy sees one
machine's traffic while the meter counts the whole account, so every beta is
biased upwards and the implied window cap is a floor; the *ratios* between
betas are what the fit is good for.

    uv run optime.py                       # every meter, fit + summary
    uv run optime.py --meter 5h --show     # print the interval table
    uv run optime.py --csv ticks.csv       # dump the dataset
    uv run optime.py --by-model            # one beta per (model, counter)
    uv run optime.py --since 2026-09-16    # ignore signal before a date

The script is agnostic to how much data the database holds; rerun it any
time and it refits on everything recorded.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from proxy import DB_PATH, PRICE, connect, credits

# meter key -> (utilisation column, reset column, label)
METERS = {
    "5h": ("five_h_utilization", "five_h_reset", "5-hour"),
    "7d": ("seven_d_utilization", "seven_d_reset", "7-day"),
    "opus": ("opus_utilization", "opus_reset", "7-day Opus/Fable"),
}
# counter key -> requests column
COUNTERS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_write": "cache_creation_tokens",
    "cache_read": "cache_read_tokens",
}
# What proxy.credits() assumes each counter is worth, relative to input.
CREDIT_WEIGHTS = {"input": 1.0, "output": 5.0, "cache_write": 1.0, "cache_read": 0.0}

MTOK = 1_000_000


# --------------------------------------------------------------------------- #
# Dataset construction
# --------------------------------------------------------------------------- #

@dataclass
class Interval:
    meter: str
    reset: int | None
    t0: int
    t1: int
    lvl0: int
    lvl1: int
    n: int = 0
    # model -> [input, output, cache_write, cache_read]
    tokens: dict[str, list[int]] = field(default_factory=dict)
    credits: int = 0

    @property
    def delta(self) -> int:
        return self.lvl1 - self.lvl0

    def total(self, counter: str) -> int:
        i = list(COUNTERS).index(counter)
        return sum(v[i] for v in self.tokens.values())


@dataclass
class MeterData:
    key: str
    windows: int = 0
    boundaries: int = 0
    censored: int = 0     # first level of each window: no tick into it observed
    empty: int = 0        # no recorded request between two ticks
    intervals: list[Interval] = field(default_factory=list)


def _local(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else "-"


def _parse_when(text: str | None) -> int | None:
    if not text:
        return None
    return int(datetime.fromisoformat(text).timestamp())


def boundaries(rows):
    """Per window, the first sighting of each new running-max level.

    Rows are (ts, util, reset) sorted by header-generation time. Returns
    [(reset, [(ts, level)])] in first-seen window order.
    """
    windows: dict = {}
    order = []
    for ts, util, reset in rows:
        level = round(util * 100)
        if reset not in windows:
            windows[reset] = []
            order.append(reset)
        seen = windows[reset]
        if not seen or level > seen[-1][1]:
            seen.append((ts, level))
    return [(reset, windows[reset]) for reset in order]


class Requests:
    """All priced requests, sorted by completion time, sliceable by interval."""

    def __init__(self, conn):
        rows = conn.execute(
            "SELECT ts, model, input_tokens, output_tokens, cache_creation_tokens, "
            "cache_read_tokens FROM requests WHERE model IS NOT NULL AND model != '' "
            "ORDER BY ts").fetchall()
        self.ts = np.array([r["ts"] for r in rows], dtype=np.int64)
        self.rows = rows
        # request_id -> start time, for placing a signal row where its header
        # was generated rather than where the response finished.
        self.started = {r["request_id"]: r["ts"] - (r["duration_ms"] or 0) // 1000
                        for r in conn.execute(
                            "SELECT request_id, ts, duration_ms FROM requests "
                            "WHERE request_id IS NOT NULL")}

    def fill(self, iv: Interval):
        lo = int(np.searchsorted(self.ts, iv.t0, side="right"))
        hi = int(np.searchsorted(self.ts, iv.t1, side="right"))
        for r in self.rows[lo:hi]:
            counts = iv.tokens.setdefault(r["model"], [0, 0, 0, 0])
            for k, col in enumerate(COUNTERS.values()):
                counts[k] += r[col] or 0
            iv.credits += credits(r["model"], r["input_tokens"] or 0,
                                  r["output_tokens"] or 0,
                                  r["cache_creation_tokens"] or 0)
            iv.n += 1


def build(conn, meter: str, requests: Requests, since: int | None,
          until: int | None) -> MeterData:
    util_col, reset_col, _ = METERS[meter]
    have_id = "request_id" in {
        r[1] for r in conn.execute("PRAGMA table_info(ratelimit)")}
    id_col = "request_id" if have_id else "NULL"
    sql = (f"SELECT ts, {util_col} u, {reset_col} r, {id_col} rid FROM ratelimit "
           f"WHERE {util_col} IS NOT NULL")
    params: list = []
    if since is not None:
        sql += " AND ts >= ?"
        params.append(since)
    if until is not None:
        sql += " AND ts <= ?"
        params.append(until)
    rows = [(requests.started.get(r["rid"], r["ts"]), r["u"], r["r"]) for r in
            conn.execute(sql + " ORDER BY ts, id", params)]
    rows.sort(key=lambda row: row[0])
    data = MeterData(meter)
    for reset, ticks in boundaries(rows):
        data.windows += 1
        data.boundaries += len(ticks)
        data.censored += 1
        # ticks[0] is the level the window was first seen at; the tick into it
        # was not observed, so it can start no interval.
        for (t0, l0), (t1, l1) in zip(ticks[1:], ticks[2:]):
            iv = Interval(meter, reset, t0, t1, l0, l1)
            requests.fill(iv)
            if iv.n == 0:
                data.empty += 1
                continue
            data.intervals.append(iv)
    return data


# --------------------------------------------------------------------------- #
# Regression
# --------------------------------------------------------------------------- #

@dataclass
class Fit:
    names: list[str]
    beta: np.ndarray      # ticks per Mtok
    se: np.ndarray
    n: int
    rank: int
    r2: float
    rmse: float
    resid: np.ndarray


def design(intervals: list[Interval], counters: list[str], by_model: bool):
    if by_model:
        totals = {}
        for iv in intervals:
            for model, counts in iv.tokens.items():
                totals[model] = totals.get(model, 0) + sum(counts)
        models = sorted(totals, key=lambda m: -totals[m])
        names = [f"{m}:{c}" for m in models for c in counters]
        idx = [list(COUNTERS).index(c) for c in counters]
        X = np.array([[iv.tokens.get(m, [0, 0, 0, 0])[k] for m in models for k in idx]
                      for iv in intervals], dtype=float)
    else:
        names = list(counters)
        X = np.array([[iv.total(c) for c in counters] for iv in intervals], dtype=float)
    y = np.array([iv.delta for iv in intervals], dtype=float)
    return names, X / MTOK, y


def ols(names: list[str], X: np.ndarray, y: np.ndarray) -> Fit:
    """No-intercept least squares with classical standard errors."""
    n, p = X.shape
    beta, _, rank, _ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    rss = float(resid @ resid)
    dof = max(n - rank, 1)
    sigma2 = rss / dof
    cov = sigma2 * np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    r2 = 1 - rss / float(y @ y) if y.any() else float("nan")
    return Fit(names, beta, se, n, rank, r2, float(np.sqrt(rss / n)), resid)


def fit_meter(data: MeterData, counters: list[str], by_model: bool) -> Fit | None:
    if not data.intervals:
        return None
    names, X, y = design(data.intervals, counters, by_model)
    if X.shape[0] <= X.shape[1]:
        print(f"  {X.shape[0]} intervals for {X.shape[1]} coefficients: "
              f"not enough data to fit yet")
        return None
    return ols(names, X, y)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def report_fit(fit: Fit, data: MeterData):
    base = fit.names.index("input") if "input" in fit.names else None
    b_in = fit.beta[base] if base is not None and fit.beta[base] > 0 else None
    print(f"  {'counter':<32} {'ticks/Mtok':>11} {'se':>9} {'t':>6} "
          f"{'tok/tick':>12} {'x input':>8} {'credits()':>9}")
    for name, b, s in zip(fit.names, fit.beta, fit.se):
        t = b / s if s > 0 else float("nan")
        per_tick = f"{MTOK / b:>12,.0f}" if b > 0 else f"{'-':>12}"
        rel = f"{b / b_in:>8.2f}" if b_in else f"{'-':>8}"
        counter = name.split(":")[-1]
        assumed = f"{CREDIT_WEIGHTS[counter]:>9.1f}" if base is not None else f"{'-':>9}"
        flag = "  <- negative" if b < 0 else ""
        print(f"  {name:<32} {b:>11.3f} {s:>9.3f} {t:>6.1f} {per_tick} {rel} {assumed}{flag}")
    ticks = sum(iv.delta for iv in data.intervals)
    note = "" if fit.rank == len(fit.names) else f"  RANK DEFICIENT ({fit.rank})"
    print(f"  n={fit.n} intervals, {ticks} ticks, p={len(fit.names)}, "
          f"R2={fit.r2:.3f}, rmse={fit.rmse:.2f} ticks{note}")


def report_credit_fit(data: MeterData):
    """One-coefficient fit on proxy.credits(): what a tick costs in credits."""
    c = np.array([iv.credits for iv in data.intervals], dtype=float)
    y = np.array([iv.delta for iv in data.intervals], dtype=float)
    if not c.any():
        return
    gamma = float(c @ y / (c @ c))
    resid = y - gamma * c
    rmse = float(np.sqrt(resid @ resid / len(y)))
    per_tick = 1 / gamma
    print(f"  credits() as the single regressor: 1 tick = {per_tick:,.0f} credits "
          f"(rmse {rmse:.2f} ticks)\n"
          f"  -> window cap >= {100 * per_tick:,.0f} credits (a floor: the proxy "
          f"sees one machine)")


def show_intervals(data: MeterData, fit: Fit | None):
    print(f"  {'window resets':<18} {'lvl':>7} {'d':>2} {'from':<18} {'to':<18} "
          f"{'mins':>6} {'req':>4} {'input':>9} {'output':>9} {'cache_w':>10} "
          f"{'cache_r':>11} {'credits':>10} {'resid':>6}")
    for k, iv in enumerate(data.intervals):
        resid = f"{fit.resid[k]:>6.2f}" if fit else f"{'':>6}"
        print(f"  {_local(iv.reset):<18} {iv.lvl0:>3}>{iv.lvl1:<3} {iv.delta:>2} "
              f"{_local(iv.t0):<18} {_local(iv.t1):<18} {(iv.t1 - iv.t0) / 60:>6.1f} "
              f"{iv.n:>4} {iv.total('input'):>9,} {iv.total('output'):>9,} "
              f"{iv.total('cache_write'):>10,} {iv.total('cache_read'):>11,} "
              f"{iv.credits:>10,} {resid}")


def write_csv(path: Path, datasets: list[MeterData]):
    models = sorted({m for d in datasets for iv in d.intervals for m in iv.tokens})
    head = ["meter", "reset", "t_from", "t_to", "level_from", "level_to", "ticks",
            "requests", "credits", *COUNTERS]
    head += [f"{m}:{c}" for m in models for c in COUNTERS]
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        for d in datasets:
            for iv in d.intervals:
                row = [d.key, iv.reset, iv.t0, iv.t1, iv.lvl0, iv.lvl1, iv.delta,
                       iv.n, iv.credits, *(iv.total(c) for c in COUNTERS)]
                for m in models:
                    row += iv.tokens.get(m, [0, 0, 0, 0])
                w.writerow(row)
    rows = sum(len(d.intervals) for d in datasets)
    print(f"wrote {rows} intervals to {path}")


# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--meter", choices=list(METERS), action="append",
                    help="which meter(s); default all")
    ap.add_argument("--since", help="ignore signal before this local ISO date/time")
    ap.add_argument("--until", help="ignore signal after this local ISO date/time")
    ap.add_argument("--counters", default=",".join(COUNTERS),
                    help=f"regressors, comma separated from {','.join(COUNTERS)}")
    ap.add_argument("--by-model", action="store_true",
                    help="one coefficient per (model, counter)")
    ap.add_argument("--show", action="store_true", help="print the interval table")
    ap.add_argument("--csv", type=Path, help="write the interval dataset here")
    args = ap.parse_args(argv)

    counters = [c.strip() for c in args.counters.split(",") if c.strip()]
    bad = [c for c in counters if c not in COUNTERS]
    if bad:
        ap.error(f"unknown counter(s): {', '.join(bad)}")
    if not args.db.exists():
        print("no database yet", file=sys.stderr)
        return 1

    since, until = _parse_when(args.since), _parse_when(args.until)
    with connect(args.db, readonly=True) as conn:
        requests = Requests(conn)
        datasets = [build(conn, m, requests, since, until)
                    for m in (args.meter or list(METERS))]

    unpriced = sorted({m for d in datasets for iv in d.intervals
                       for m in iv.tokens if m not in PRICE})
    if unpriced:
        print(f"note: credits() prices nothing for {', '.join(unpriced)}; their "
              f"tokens still enter the token fit\n")

    for data in datasets:
        label = METERS[data.key][2]
        print(f"== {data.key} ({label}): {data.windows} windows, "
              f"{data.boundaries} tick boundaries -> {len(data.intervals)} usable "
              f"intervals ({data.censored} censored window starts, "
              f"{data.empty} with no recorded traffic dropped)")
        if not data.intervals:
            print("  nothing to fit\n")
            continue
        fit = fit_meter(data, counters, args.by_model)
        if fit:
            report_fit(fit, data)
        report_credit_fit(data)
        if args.show:
            print()
            show_intervals(data, fit)
        print()

    if args.csv:
        write_csv(args.csv, datasets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
