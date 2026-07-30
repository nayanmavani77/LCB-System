"""Data access + tick replay for the L-Rev engine.

replay_window() is what the backtest runner uses: it streams historical TBBO
ticks through the SAME LRevStrategy/PaperBroker code path that live trading
uses. Contract rolls flatten positions and reset strategy state, matching how
the research study treated segment boundaries.
"""
from __future__ import annotations

import json
import os

import pandas as pd

from engines.lrev import Bar, LRevStrategy, TF_SECONDS

from .broker import PaperBroker

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.environ.get("LCB_CACHE", os.path.join(_REPO, "data_cache"))


def ns_index(idx):
    """Epoch NANOSECONDS for a DatetimeIndex, whatever unit it carries.

    pandas 2/3 keep a per-index resolution (s/ms/us/ns), so a bare
    `idx.view("int64")` returns the raw counter in the index's OWN unit. A
    parquet or resample result that comes back as datetime64[us] then yields
    numbers 1000x too small, and every comparison against a nanosecond
    cutoff silently inverts: instead of excluding post-cutoff bars it lets
    them all through. That is a leakage bug that no test on a ns-precision
    fixture can catch, so normalize the unit here, once."""
    try:
        idx = idx.as_unit("ns")          # pandas >= 2.0
    except AttributeError:
        pass                             # pandas 1.x: always ns already
    return idx.view("int64")


def cache_for(symbol: str = "GC", base: str | None = None) -> str:
    """Per-symbol cache dir: data_cache/<SYM>. GC falls back to the legacy
    flat data_cache/ layout if the per-symbol dir doesn't exist yet."""
    root = base or CACHE
    per = os.path.join(root, symbol.upper())
    if os.path.exists(os.path.join(per, "segments.json")):
        return per
    if symbol.upper() == "GC" and os.path.exists(os.path.join(root, "segments.json")):
        return root  # legacy layout
    return per


def load_segments(cache: str = CACHE):
    with open(os.path.join(cache, "segments.json")) as f:
        return json.load(f)


def data_bounds(cache: str = CACHE):
    segs = load_segments(cache)
    return (pd.Timestamp(min(s["start"] for s in segs), tz="UTC"),
            pd.Timestamp(max(s["end"] for s in segs), tz="UTC"))


def seed_warmup(strat: LRevStrategy, sym: str, cutoff_ns: int, cache: str = CACHE):
    """Preload closed bars from before cutoff so levels/MTR have history.

    A bar is included only if it CLOSED (open + tf) at or before the cutoff:
    bar timestamps are open times, so filtering on open < cutoff alone would
    let a bar that SPANS a non-aligned cutoff leak a few post-cutoff minutes
    into the warmup."""
    for tf in TF_SECONDS:
        bars = pd.read_parquet(os.path.join(cache, "seg", f"{sym}_{tf}.parquet"))
        bt = ns_index(bars.index)
        pre = bars[bt + TF_SECONDS[tf] * 1_000_000_000 <= cutoff_ns]
        strat.seed_bars(tf, [Bar(int(t), r.open, r.high, r.low, r.close, r.volume)
                             for t, r in zip(ns_index(pre.index),
                                             pre.itertuples())])


def seed_warmup_full(strat, segs, cutoff_ns: int, cache: str = CACHE):
    """Warmup for engines that need LONG history (WANTS_FULL_HISTORY): seed
    bars across ALL contract segments (each clipped to its front-month
    window) up to cutoff - a continuous front-month bar series spanning
    rolls. Engines declare which timeframes they want via WARMUP_TFS."""
    tfs = getattr(type(strat), "WARMUP_TFS", tuple(TF_SECONDS))
    for tf in tfs:
        tf_ns = TF_SECONDS[tf] * 1_000_000_000
        frames = []
        for seg in segs:
            s0 = pd.Timestamp(seg["start"], tz="UTC").value
            s1 = min(pd.Timestamp(seg["end"], tz="UTC").value, cutoff_ns)
            if s1 <= s0:
                continue
            p = os.path.join(cache, "seg", f"{seg['symbol']}_{tf}.parquet")
            if not os.path.exists(p):
                continue
            b = pd.read_parquet(p)
            bt = ns_index(b.index)
            # only bars fully CLOSED by the clip point (open times + tf);
            # a bar spanning a non-aligned cutoff must not seed the warmup
            frames.append(b[(bt >= s0) & (bt + tf_ns <= s1)])
        if not frames:
            continue
        bars = pd.concat(frames).sort_index()
        bars = bars[~bars.index.duplicated(keep="last")]
        strat.seed_bars(tf, [Bar(int(t), r.open, r.high, r.low, r.close, r.volume)
                             for t, r in zip(ns_index(bars.index),
                                             bars.itertuples())])


def replay_window(start=None, end=None, config: dict | None = None,
                  cache: str = CACHE, trade_log_path=None,
                  log=None, progress=print, strategy_cls=None,
                  cost_pts=0.4, point_value=100.0):
    """Replay [start, end) through the engine. Returns the PaperBroker."""
    silent = (lambda *a, **k: None)
    log = log or silent
    segs = load_segments(cache)
    def _ns(x, default):
        if x is None:
            return default
        t = pd.Timestamp(x)
        t = t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")
        return t.value
    t0 = _ns(start, 0)
    t1 = _ns(end, 2**63 - 1)

    broker = PaperBroker(trade_log_path=trade_log_path, log=log,
                         cost_pts=cost_pts, point_value=point_value)
    total = 0
    for seg in segs:
        seg_t0 = pd.Timestamp(seg["start"], tz="UTC").value
        seg_t1 = pd.Timestamp(seg["end"], tz="UTC").value
        lo, hi = max(seg_t0, t0), min(seg_t1, t1)
        if hi <= lo:
            continue
        sym = seg["symbol"]
        cls = strategy_cls or LRevStrategy
        strat = cls(broker, config=config, log=log)
        if getattr(cls, "WANTS_FULL_HISTORY", False):
            seed_warmup_full(strat, segs, lo, cache)
        else:
            seed_warmup(strat, sym, lo, cache)

        tb = pd.read_parquet(os.path.join(cache, "seg", f"{sym}_tbbo.parquet"))
        tb = tb[(tb.ts >= lo) & (tb.ts < hi)]
        n = len(tb)
        if n == 0:
            continue
        progress(f"replaying {sym}: {n:,} ticks "
                 f"({pd.Timestamp(lo)} .. {pd.Timestamp(hi)})")
        ts_a = tb.ts.tolist()
        px_a = tb.price.tolist()
        sz_a = tb["size"].tolist()
        sd_a = [chr(x) for x in tb.side.tolist()]
        bid_a = tb.bid.tolist()
        ask_a = tb.ask.tolist()
        b_tick = broker.on_tick
        s_tick = strat.on_tick
        for i in range(n):
            b_tick(ts_a[i], bid_a[i], ask_a[i])
            s_tick(ts_a[i], px_a[i], sz_a[i], sd_a[i], bid_a[i], ask_a[i])
        broker.close_all(ts_a[-1])  # contract roll / window end: flatten
        total += n
    progress(f"replayed {total:,} ticks total")
    return broker
