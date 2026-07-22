"""Generate rich research logs: trades + TBBO features + forward returns + context."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
import engine as E

MIN = 60_000_000_000
HORIZONS = {"5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1h": 60 * MIN,
            "2h": 120 * MIN, "4h": 240 * MIN, "8h": 480 * MIN, "24h": 1440 * MIN}


def add_context(trades, seg):
    """Forward mid returns, richer flow features, trend state, session."""
    if not trades:
        return trades
    mid = (seg.bid + seg.ask) / 2.0
    h1 = seg.bars["H1"]
    h4 = seg.bars["H4"]
    # H1 sma20 / H4 sma20 trend
    c1 = pd.Series(h1["c"]).rolling(20).mean().to_numpy()
    c4 = pd.Series(h4["c"]).rolling(20).mean().to_numpy()
    for tr in trades:
        i = tr["i_fill"]
        m0 = mid[i]
        t0 = seg.ts[i]
        for lbl, dt in HORIZONS.items():
            j = np.searchsorted(seg.ts, t0 + dt, side="right") - 1
            tr[f"fwd_{lbl}"] = (mid[min(j, len(mid) - 1)] - m0) * tr["dir"] if j >= i else np.nan
        # flow features around entry
        for lbl, dt in (("30s", MIN // 2), ("2m", 2 * MIN), ("10m", 10 * MIN), ("60m", 60 * MIN)):
            v = seg.win_sum(seg.cum_v, i + 1, dt)
            sv = seg.win_sum(seg.cum_sv, i + 1, dt)
            tr[f"imb_{lbl}"] = sv / v if v > 0 else 0.0
            tr[f"vol_{lbl}"] = v
        # trade intensity ratio: vol last 5m vs avg 5m vol over last 2h
        v5 = seg.win_sum(seg.cum_v, i + 1, 5 * MIN)
        v2h = seg.win_sum(seg.cum_v, i + 1, 120 * MIN)
        tr["intensity"] = v5 / (v2h / 24.0) if v2h > 0 else 0.0
        # book imbalance at trigger
        tr["qimb"] = 0.0
        # trend state at entry
        k1 = np.searchsorted(h1["t"], t0, side="right") - 2  # last closed H1 bar
        k4 = np.searchsorted(h4["t"], t0, side="right") - 2
        tr["trend_h1"] = np.sign(h1["c"][k1] - c1[k1]) if k1 >= 19 else 0.0
        tr["trend_h4"] = np.sign(h4["c"][k4] - c4[k4]) if k4 >= 19 else 0.0
        tr["with_trend_h4"] = tr["trend_h4"] * tr["dir"]
        tr["with_trend_h1"] = tr["trend_h1"] * tr["dir"]
        ts = pd.Timestamp(t0)
        tr["hour"] = ts.hour
        tr["dow"] = ts.dayofweek
        # R-multiple of pnl
        tr["r"] = tr["pnl"] / tr["sld"] if tr["sld"] > 0 else np.nan
    return trades


def main():
    segs = E.load_all()
    l_all, cb_all = [], []
    for sname, seg in segs.items():
        for tf, slm in [("M15", 1.5), ("H1", 0.5), ("H4", 0.5)]:
            tr = E.run_l(seg, tf, sl_mult=slm, rr=2.0)
            add_context(tr, seg)
            l_all += tr
            tc = E.run_cb(seg, tf, sl_mult=slm, rr=2.0, log_all_signals=True,
                          min_body_research=1.5)
            add_context(tc, seg)
            cb_all += tc
        print(sname, "done: L", len(l_all), "CB", len(cb_all))
    pd.DataFrame(l_all).to_parquet(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "l_research.parquet"))
    pd.DataFrame(cb_all).to_parquet(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "cb_research.parquet"))


if __name__ == "__main__":
    main()
