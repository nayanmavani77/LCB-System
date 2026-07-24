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

from .broker import PaperBroker
from .strategy import Bar, LRevStrategy, TF_SECONDS

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.environ.get("LCB_CACHE", os.path.join(_REPO, "data_cache"))


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
    """Preload closed bars from before cutoff so levels/MTR have history."""
    for tf in TF_SECONDS:
        bars = pd.read_parquet(os.path.join(cache, "seg", f"{sym}_{tf}.parquet"))
        bt = bars.index.view("int64")
        pre = bars[bt < cutoff_ns]
        strat.seed_bars(tf, [Bar(int(t), r.open, r.high, r.low, r.close, r.volume)
                             for t, r in zip(pre.index.view("int64"),
                                             pre.itertuples())])


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
