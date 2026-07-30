"""Integrity check for an already-built tick cache.

The prep step now drops invalid quotes at decode time, but a cache built
BEFORE that fix can still contain them - and a bad quote in the cache does
not look like an error, it looks like a trade. This script tells you whether
your existing cache is clean without rebuilding anything.

    python scripts/check_cache.py                 # GC
    python scripts/check_cache.py --symbol SI

What it looks for, and why each one matters:

  undefined / crossed quotes   Databento encodes an undefined price as
        INT64_MAX, which becomes ~9.223e9 after scaling. The PaperBroker
        will happily fill against it, so one such row can invent a
        multi-billion-point trade in an equity curve; a crossed book
        (ask < bid) produces a negative spread that passes every gate.

  bar index resolution         pandas 2/3 carry a per-index unit. If a bar
        parquet reads back as datetime64[us] instead of [ns], any raw
        .view("int64") is 1000x too small and warmup cutoff comparisons
        stop excluding anything - i.e. silent look-ahead. core.data.ns_index
        normalizes this now; this check tells you which files are affected.

  non-monotonic / duplicate    ticks must be replayable in order; duplicate
        bar timestamps mean a roll stitched two contracts onto one bar.

  segment coverage             a segment whose tick file is missing or whose
        ticks do not span its declared window silently shortens a backtest.

Exit code is 1 if anything suspicious was found, so it can gate a build.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.data import cache_for, ns_index

LIMIT = 1e7          # $10m/oz - above this it is an encoding, not a price


def check_ticks(path):
    """Returns (n, problems) for one *_tbbo.parquet."""
    df = pd.read_parquet(path, columns=["ts", "price", "bid", "ask"])
    n = len(df)
    bad = {}
    if not n:
        return 0, {"empty file": 1}
    for col in ("price", "bid", "ask"):
        huge = int((df[col].abs() >= LIMIT).sum())
        if huge:
            bad[f"{col} >= 1e7 (undefined/INT64_MAX)"] = huge
        nonpos = int((df[col] <= 0).sum())
        if nonpos:
            bad[f"{col} <= 0"] = nonpos
        nan = int(df[col].isna().sum())
        if nan:
            bad[f"{col} is NaN"] = nan
    crossed = int((df["ask"] < df["bid"]).sum())
    if crossed:
        bad["ask < bid (crossed book)"] = crossed
    unsorted = int((df["ts"].diff() < 0).sum())
    if unsorted:
        bad["ts goes backwards"] = unsorted
    return n, bad


def check_bars(path):
    bars = pd.read_parquet(path)
    bad = {}
    unit = str(bars.index.dtype)
    if "[ns" not in unit:
        bad[f"index resolution is {unit}, not nanoseconds"] = 1
    bt = ns_index(bars.index)
    if len(bt) and (pd.Series(bt).diff() < 0).any():
        bad["bar timestamps go backwards"] = 1
    dupes = int(bars.index.duplicated().sum())
    if dupes:
        bad["duplicate bar timestamps"] = dupes
    for col in ("open", "high", "low", "close"):
        if col in bars.columns:
            n = int(((bars[col] <= 0) | (bars[col] >= LIMIT)
                     | bars[col].isna()).sum())
            if n:
                bad[f"{col} out of range or NaN"] = n
    if {"high", "low"} <= set(bars.columns):
        n = int((bars["high"] < bars["low"]).sum())
        if n:
            bad["high < low"] = n
    return len(bars), bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GC")
    args = ap.parse_args()
    cache = cache_for(args.symbol.upper())
    print(f"cache: {cache}")
    if not os.path.exists(cache):
        raise SystemExit(f"no cache at {cache} - run scripts/prep.py first")

    problems = 0
    seg_path = os.path.join(cache, "segments.json")
    segs = []
    if os.path.exists(seg_path):
        with open(seg_path) as f:
            segs = json.load(f)
        print(f"segments: {len(segs)} "
              f"({segs[0]['start'][:10]} .. {segs[-1]['end'][:10]})")
    else:
        print("WARNING: no segments.json")
        problems += 1

    print("\nticks:")
    tick_files = sorted(glob.glob(os.path.join(cache, "seg", "*_tbbo.parquet")))
    if not tick_files:
        print("  none found")
        problems += 1
    total = 0
    for p in tick_files:
        n, bad = check_ticks(p)
        total += n
        name = os.path.basename(p)
        if bad:
            problems += 1
            print(f"  {name}: {n:,} ticks  <-- PROBLEMS")
            for k, v in bad.items():
                print(f"      {v:,} x {k}")
        else:
            print(f"  {name}: {n:,} ticks  ok")
    print(f"  total {total:,} ticks")

    print("\nbars:")
    bar_files = sorted(p for p in glob.glob(os.path.join(cache, "seg", "*.parquet"))
                       if not p.endswith("_tbbo.parquet"))
    for p in bar_files:
        n, bad = check_bars(p)
        name = os.path.basename(p)
        if bad:
            problems += 1
            print(f"  {name}: {n:,} bars  <-- PROBLEMS")
            for k, v in bad.items():
                print(f"      {v} x {k}")
        else:
            print(f"  {name}: {n:,} bars  ok")

    print("\ncoverage:")
    for s in segs:
        p = os.path.join(cache, "seg", f"{s['symbol']}_tbbo.parquet")
        if not os.path.exists(p):
            print(f"  {s['symbol']}: NO tick file (backtests skip this segment)")
            problems += 1
            continue
        ts = pd.read_parquet(p, columns=["ts"])["ts"]
        if not len(ts):
            print(f"  {s['symbol']}: tick file is empty")
            problems += 1
            continue
        lo = pd.Timestamp(int(ts.iloc[0]), tz="UTC")
        hi = pd.Timestamp(int(ts.iloc[-1]), tz="UTC")
        want_lo = pd.Timestamp(s["start"], tz="UTC")
        want_hi = pd.Timestamp(s["end"], tz="UTC")
        gap_l = (lo - want_lo).total_seconds() / 3600
        gap_r = (want_hi - hi).total_seconds() / 3600
        flag = "" if max(gap_l, gap_r) < 30 else "  <-- short by >30h"
        print(f"  {s['symbol']}: {lo:%Y-%m-%d %H:%M} .. {hi:%Y-%m-%d %H:%M} "
              f"(declared {want_lo:%Y-%m-%d} .. {want_hi:%Y-%m-%d}; "
              f"missing {gap_l:.0f}h head / {gap_r:.0f}h tail){flag}")
        if flag:
            problems += 1

    if problems:
        print(f"\n{problems} FILE(S)/CHECK(S) NEED ATTENTION. Anything about "
              f"undefined quotes means the cache predates the prep.py filter: "
              f"re-run scripts/prep.py to rebuild it clean.")
        raise SystemExit(1)
    print("\ncache looks clean")


if __name__ == "__main__":
    main()
