"""Backtest engine for L-System and CB-System on GC with TBBO fill simulation.

Faithful mechanics from the MQ5 EA:
  L-System : fractal swing levels (N bars each side, detected N+1 bars later),
             GTC stop order AT the level, one order per level,
             level max age 336h from swing bar time, max distance $200,
             SL = MTR(N) * mult at placement, TP = SL * RR.
  CB-System: signal candle body >= MinNormBody * medianBody(50, excl. signal),
             stop order at high+0.10 (buy) / low-0.05 (sell),
             order expires at end of the bar following the signal candle,
             SL = MTR(50) * mult at signal, TP = SL * RR.

Fills on TBBO stream (trade + BBO at each trade):
  buy stop  triggers when ask >= px -> fill at max(px, ask)
  sell stop triggers when bid <= px -> fill at min(px, bid)
  long  exit: SL when bid <= sl (fill min(sl,bid)),  TP when bid >= tp (fill tp)
  short exit: SL when ask >= sl (fill max(sl,ask)),  TP when ask <= tp (fill tp)
  SL checked with priority on the same tick.
"""
import json
import os

import numpy as np
import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("LCB_CACHE", os.path.join(_REPO, "data_cache"))

# Segment table (front-month contract windows). prep.py derives this from the
# DBN metadata and writes data_cache/segments.json; the constants below are
# the fallback for the original 2025-12-28..2026-07-17 dataset.
SEGMENTS = ["GCG6", "GCJ6", "GCM6", "GCQ6"]
SEG_BOUNDS = {
    "GCG6": ("2025-12-28", "2026-01-30"),
    "GCJ6": ("2026-01-30", "2026-03-30"),
    "GCM6": ("2026-03-30", "2026-05-29"),
    "GCQ6": ("2026-05-29", "2026-07-18"),
}
_seg_json = os.path.join(DATA, "segments.json")
if os.path.exists(_seg_json):
    with open(_seg_json) as _f:
        _segs = json.load(_f)
    SEGMENTS = [s["symbol"] for s in _segs]
    SEG_BOUNDS = {s["symbol"]: (s["start"], s["end"]) for s in _segs}
TF_SEC = {"M15": 900, "H1": 3600, "H4": 14400}


class Seg:
    def __init__(self, sym):
        self.sym = sym
        self.bars = {}
        for tf in ("M15", "H1", "H4"):
            b = pd.read_parquet(f"{DATA}/seg/{sym}_{tf}.parquet")
            self.bars[tf] = {
                "t": np.asarray(b.index.view("int64")),  # open time ns
                "o": b["open"].to_numpy(), "h": b["high"].to_numpy(),
                "l": b["low"].to_numpy(), "c": b["close"].to_numpy(),
                "v": b["volume"].to_numpy(),
            }
        tb = pd.read_parquet(f"{DATA}/seg/{sym}_tbbo.parquet")
        self.ts = tb["ts"].to_numpy()
        self.bid = tb["bid"].to_numpy()
        self.ask = tb["ask"].to_numpy()
        self.px = tb["price"].to_numpy()
        self.sz = tb["size"].to_numpy().astype(np.int64)
        side = tb["side"].to_numpy()
        self.sign = np.where(side == ord("B"), 1, np.where(side == ord("A"), -1, 0))
        # cumulative features for windowed sums
        self.cum_sv = np.concatenate([[0], np.cumsum(self.sz * self.sign)])
        self.cum_v = np.concatenate([[0], np.cumsum(self.sz)])
        self.cum_n = np.arange(len(self.ts) + 1)
        s0, s1 = SEG_BOUNDS[sym]
        self.t0 = pd.Timestamp(s0, tz="UTC").value
        self.t1 = pd.Timestamp(s1, tz="UTC").value

    def quote_before(self, t):
        """Last quote at or before t. Returns (idx, bid, ask); idx may be -1."""
        i = np.searchsorted(self.ts, t, side="right") - 1
        if i < 0:
            return -1, np.nan, np.nan
        return i, self.bid[i], self.ask[i]

    def win_sum(self, arr_cum, i_end, dt_ns):
        """Sum of a cum-array over ts window (t_end-dt, t_end]. i_end exclusive index."""
        t_end = self.ts[i_end - 1] if i_end > 0 else 0
        i_start = np.searchsorted(self.ts, t_end - dt_ns, side="right")
        return arr_cum[i_end] - arr_cum[i_start]


def scan_first(cond_arr, i0, i1, chunk=2_000_000):
    """First index in [i0,i1) where cond_arr is True, else -1."""
    i = i0
    while i < i1:
        j = min(i + chunk, i1)
        sl = cond_arr[i:j]
        k = np.argmax(sl)
        if sl[k]:
            return i + k
        i = j
    return -1


def sim_exit(seg, direction, i_entry, sl, tp, i_max=None):
    """Scan TBBO from i_entry+1 for SL/TP exit. Returns (i_exit, px_exit, reason).
    If neither hit by i_max (or end), exit at last quote (mark)."""
    i1 = len(seg.ts) if i_max is None else min(i_max, len(seg.ts))
    i0 = i_entry + 1
    if direction > 0:
        hit_sl = seg.bid[i0:i1] <= sl
        hit_tp = seg.bid[i0:i1] >= tp
    else:
        hit_sl = seg.ask[i0:i1] >= sl
        hit_tp = seg.ask[i0:i1] <= tp
    cond = hit_sl | hit_tp
    k = scan_first(cond, 0, i1 - i0)
    if k < 0:
        j = i1 - 1
        pxe = seg.bid[j] if direction > 0 else seg.ask[j]
        return j, pxe, "eos"
    j = i0 + k
    if hit_sl[k]:  # SL priority
        pxe = min(sl, seg.bid[j]) if direction > 0 else max(sl, seg.ask[j])
        return j, pxe, "sl"
    else:
        pxe = tp
        return j, pxe, "tp"


def mtr(bars, n_idx, lookback):
    """Median true range of bars[n_idx-lookback : n_idx] (i.e. excluding bar n_idx).
    n_idx = index of the bar EXCLUDED (the EA's 'bar 1'). Returns 0 if insufficient."""
    lo = n_idx - lookback
    if lo < 1:
        return 0.0
    h = bars["h"][lo:n_idx]
    l = bars["l"][lo:n_idx]
    pc = bars["c"][lo - 1:n_idx - 1]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return float(np.median(tr))


def median_body(bars, n_idx, lookback):
    lo = n_idx - lookback
    if lo < 0:
        return 0.0
    b = np.abs(bars["c"][lo:n_idx] - bars["o"][lo:n_idx])
    return float(np.median(b))


def tbbo_features(seg, i_trig):
    """Order-flow features at TBBO index i_trig (trigger tick)."""
    f = {}
    MIN = 60_000_000_000
    for lbl, dt in (("1m", MIN), ("5m", 5 * MIN), ("15m", 15 * MIN), ("60m", 60 * MIN)):
        v = seg.win_sum(seg.cum_v, i_trig + 1, dt)
        sv = seg.win_sum(seg.cum_sv, i_trig + 1, dt)
        n = seg.win_sum(seg.cum_n, i_trig + 1, dt)
        f[f"vol_{lbl}"] = v
        f[f"sv_{lbl}"] = sv
        f[f"imb_{lbl}"] = sv / v if v > 0 else 0.0
        f[f"ntr_{lbl}"] = n
    f["spread"] = seg.ask[i_trig] - seg.bid[i_trig]
    bs, as_ = seg.bid[i_trig], seg.ask[i_trig]
    f["qimb"] = 0.0
    tot = seg.sz[i_trig]
    # book imbalance
    import math
    bsz = float(pd.NA) if False else 0
    f["qimb"] = 0.0
    return f


def tbbo_features_fast(seg, i_trig):
    MIN = 60_000_000_000
    f = {}
    for lbl, dt in (("1m", MIN), ("5m", 5 * MIN), ("30m", 30 * MIN)):
        v = seg.win_sum(seg.cum_v, i_trig + 1, dt)
        sv = seg.win_sum(seg.cum_sv, i_trig + 1, dt)
        f[f"vol_{lbl}"] = v
        f[f"imb_{lbl}"] = sv / v if v > 0 else 0.0
    f["spread"] = seg.ask[i_trig] - seg.bid[i_trig]
    return f


# ---------------------------------------------------------------- CB-System
def run_cb(seg, tf, sl_mult, rr, min_norm_body=3.0, lookback=50,
           buy_off=0.10, sell_off=0.05, log_all_signals=False,
           min_body_research=1.5):
    """Returns trade list. If log_all_signals, gate at min_body_research and
    record normBody so thresholds can be studied from the log."""
    bars = seg.bars[tf]
    tfns = TF_SEC[tf] * 1_000_000_000
    trades = []
    gate = min_body_research if log_all_signals else min_norm_body
    n = len(bars["t"])
    for i in range(lookback + 1, n - 1):
        # signal bar = i, evaluated at its close time
        t_sig_close = bars["t"][i] + tfns
        if not (seg.t0 <= t_sig_close < seg.t1):
            continue
        o, c, h, l = bars["o"][i], bars["c"][i], bars["h"][i], bars["l"][i]
        body = abs(c - o)
        mb = median_body(bars, i, lookback)
        if mb <= 0:
            continue
        nb = body / mb
        if nb < gate:
            continue
        m = mtr(bars, i, lookback)
        if m <= 0:
            continue
        sld = m * sl_mult
        tpd = sld * rr
        direction = 1 if c > o else (-1 if c < o else 0)
        if direction == 0:
            continue
        px = h + buy_off if direction > 0 else l - sell_off
        # order active from signal close until end of next bar.
        # EA expiry: bt + PeriodSeconds - 1 with bt = open of forming bar.
        t_exp = bars["t"][i] + 2 * tfns
        i0 = np.searchsorted(seg.ts, t_sig_close, side="left")
        i_exp = np.searchsorted(seg.ts, t_exp, side="left")
        if direction > 0:
            k = scan_first(seg.ask[i0:i_exp] >= px, 0, i_exp - i0)
        else:
            k = scan_first(seg.bid[i0:i_exp] <= px, 0, i_exp - i0)
        if k < 0:
            continue
        i_fill = i0 + k
        fill = max(px, seg.ask[i_fill]) if direction > 0 else min(px, seg.bid[i_fill])
        sl = fill - sld if direction > 0 else fill + sld
        tp = fill + tpd if direction > 0 else fill - tpd
        # note: EA sets SL/TP from px not fill; use px-based to be faithful
        sl = px - sld if direction > 0 else px + sld
        tp = px + tpd if direction > 0 else px - tpd
        i_ex, px_ex, reason = sim_exit(seg, direction, i_fill, sl, tp)
        pnl = (px_ex - fill) * direction
        tr = {"sys": "CB", "seg": seg.sym, "tf": tf, "dir": direction,
              "t_sig": t_sig_close, "t_entry": seg.ts[i_fill], "entry": fill,
              "px": px, "sl": sl, "tp": tp, "t_exit": seg.ts[i_ex],
              "exit": px_ex, "reason": reason, "pnl": pnl,
              "norm_body": nb, "mtr": m, "sld": sld,
              "i_fill": i_fill, "i_sig0": i0}
        tr.update(tbbo_features_fast(seg, i_fill))
        # features at signal close (decision time) too
        fsig = tbbo_features_fast(seg, max(i0 - 1, 0))
        tr.update({f"sig_{k2}": v2 for k2, v2 in fsig.items()})
        trades.append(tr)
    return trades


# ---------------------------------------------------------------- L-System
def run_l(seg, tf, sl_mult, rr, fract_bars=8, max_dist=200.0,
          max_age_h=336, log_features=True):
    bars = seg.bars[tf]
    tfns = TF_SEC[tf] * 1_000_000_000
    n = len(bars["t"])
    trades = []
    age_ns = max_age_h * 3600 * 1_000_000_000
    fb = fract_bars
    for i in range(fb, n - fb):
        # candidate bar i confirmed when bar i+fb closes -> detection at open
        # of bar i+fb+1; EA detects on new bar with shift=fb+1.
        t_detect = bars["t"][i + fb] + tfns  # close of bar i+fb
        if not (seg.t0 <= t_detect < seg.t1):
            continue
        ch, cl = bars["h"][i], bars["l"][i]
        hs = bars["h"][i - fb:i + fb + 1]
        ls = bars["l"][i - fb:i + fb + 1]
        is_high = np.all(hs <= ch) and np.sum(hs == ch) == 1
        is_low = np.all(ls >= cl) and np.sum(ls == cl) == 1
        # EA: strict inequality on neighbors; equal-extreme neighbors keep both
        is_high = not np.any(np.delete(hs, fb) > ch)
        is_low = not np.any(np.delete(ls, fb) < cl)
        t_formed = bars["t"][i]
        t_dead = t_formed + age_ns
        if t_dead <= t_detect:
            continue
        for isLow, level in ((True, cl), (False, ch)) if (is_low or is_high) else ():
            if isLow and not is_low:
                continue
            if not isLow and not is_high:
                continue
            iq, qb, qa = seg.quote_before(t_detect)
            if iq < 0:
                iq = np.searchsorted(seg.ts, t_detect, side="left")
                if iq >= len(seg.ts):
                    continue
                qb, qa = seg.bid[iq], seg.ask[iq]
            spread = qa - qb
            # already broken?
            if isLow and qb < level - 0.01:
                continue
            if not isLow and qb > level + 0.01:
                continue
            dist = (qb - level) if isLow else (level - qb)
            if dist <= 0 or dist > max_dist:
                continue  # (EA would retry; rare for GC given $200 window)
            m = mtr(bars, i + fb + 1 if i + fb + 1 <= n else n, fract_bars)
            # MTR at detection time: bar1 = last closed = i+fb -> excluded idx = i+fb
            m = mtr(bars, i + fb, fract_bars)
            if m <= 0:
                continue
            sld = m * sl_mult
            tpd = sld * rr
            if isLow:
                px = level
                sl = px + sld
                tp = px - tpd
                direction = -1
            else:
                px = level + spread
                sl = px - sld
                tp = px + tpd
                direction = 1
            i0 = np.searchsorted(seg.ts, t_detect, side="left")
            i_dead = np.searchsorted(seg.ts, min(t_dead, seg.t1), side="left")
            if i0 >= i_dead:
                continue
            if direction > 0:
                k = scan_first(seg.ask[i0:i_dead] >= px, 0, i_dead - i0)
            else:
                k = scan_first(seg.bid[i0:i_dead] <= px, 0, i_dead - i0)
            if k < 0:
                continue
            i_fill = i0 + k
            fill = max(px, seg.ask[i_fill]) if direction > 0 else min(px, seg.bid[i_fill])
            i_ex, px_ex, reason = sim_exit(seg, direction, i_fill, sl, tp)
            pnl = (px_ex - fill) * direction
            tr = {"sys": "L", "seg": seg.sym, "tf": tf, "dir": direction,
                  "t_sig": t_detect, "t_entry": seg.ts[i_fill], "entry": fill,
                  "px": px, "sl": sl, "tp": tp, "t_exit": seg.ts[i_ex],
                  "exit": px_ex, "reason": reason, "pnl": pnl,
                  "level": level, "mtr": m, "sld": sld,
                  "wait_h": (seg.ts[i_fill] - t_detect) / 3.6e12,
                  "i_fill": i_fill}
            if log_features:
                tr.update(tbbo_features_fast(seg, i_fill))
            trades.append(tr)
    return trades


def summarize(trades, cost_pts=0.15):
    """cost_pts: commission+slippage per round turn in points
    (spread already paid via ask/bid fills). GC: $100/point/contract."""
    if not trades:
        return {"n": 0}
    df = pd.DataFrame(trades)
    df["pnl_net"] = df["pnl"] - cost_pts
    g = {
        "n": len(df),
        "win%": 100 * (df.pnl_net > 0).mean(),
        "pnl_pts": df.pnl_net.sum(),
        "pnl_$": df.pnl_net.sum() * 100,
        "avg_pts": df.pnl_net.mean(),
        "pf": df.pnl_net[df.pnl_net > 0].sum() / max(1e-9, -df.pnl_net[df.pnl_net < 0].sum()),
        "maxdd_pts": (df.pnl_net.cumsum().cummax() - df.pnl_net.cumsum()).max(),
    }
    return g


def load_all():
    return {s: Seg(s) for s in SEGMENTS}
