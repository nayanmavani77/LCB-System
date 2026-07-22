"""Strategy variants: L-Rev (filtered L-System) and CB-Fade (inverted CB with flow gate)."""
import sys
sys.path.insert(0, "/home/claude/lcb/scripts")
import numpy as np
import pandas as pd
import engine as E

MIN = 60_000_000_000
IS_SEGS = ["GCG6", "GCJ6", "GCM6"]
OOS_SEGS = ["GCQ6"]


def trend_arrays(seg):
    h4 = seg.bars["H4"]
    sma = pd.Series(h4["c"]).rolling(20).mean().to_numpy()
    return h4["t"], np.sign(h4["c"] - sma)


def trend_at(t4, tr4, t):
    k = np.searchsorted(t4, t, side="right") - 2  # last CLOSED h4 bar
    if k < 19:
        return 0.0
    return tr4[k]


def run_l_rev(seg, tf, sl_mult, rr, fract_bars=8,
              allowed=(("+1", "-1"),),   # (dir, trend) combos as strings
              max_wait_h=None, max_spread=None,
              hours=None,                # set of allowed entry hours (UTC)
              flow_lo=None, flow_hi=None,  # aligned 30s imbalance band at trigger
              max_age_h=336, max_dist=200.0):
    """L-System with entry-side/trend/quality gates. Trigger-time gates model
    the EA pulling its pending orders when conditions are bad (spread/hours),
    or a synthetic-stop implementation for the flow gate."""
    base = E.run_l(seg, tf, sl_mult, rr, fract_bars=fract_bars,
                   max_dist=max_dist, max_age_h=max_age_h, log_features=False)
    t4, tr4 = trend_arrays(seg)
    out = []
    allowset = {(int(d), int(tv)) for d, tv in allowed}
    for tr in base:
        d = tr["dir"]
        tv = trend_at(t4, tr4, tr["t_sig"])  # trend at DETECTION time (EA decides placement)
        if (d, int(tv)) not in allowset:
            continue
        if max_wait_h is not None and tr["wait_h"] > max_wait_h:
            continue
        i = tr["i_fill"]
        if max_spread is not None and (seg.ask[i] - seg.bid[i]) > max_spread:
            continue
        if hours is not None:
            hr = pd.Timestamp(tr["t_entry"]).hour
            if hr not in hours:
                continue
        if flow_lo is not None or flow_hi is not None:
            v = seg.win_sum(seg.cum_v, i + 1, MIN // 2)
            sv = seg.win_sum(seg.cum_sv, i + 1, MIN // 2)
            aimb = (sv / v if v > 0 else 0.0) * d
            if flow_lo is not None and aimb < flow_lo:
                continue
            if flow_hi is not None and aimb > flow_hi:
                continue
        tr["trend_h4"] = tv
        out.append(tr)
    return out


def run_cb_fade(seg, tf, sl_mult, rr, lookback=50, min_norm_body=3.0,
                buy_off=0.10, sell_off=0.05,
                flow_gate=None,      # fade only if aligned(breakout) 30s imb < gate
                time_stop_h=None,    # exit at market after H hours if no SL/TP
                hours=None, max_spread=None):
    """Inverted CB: when the breakout stop would fill, take the OPPOSITE side.
    dir below = FADE direction. SL/TP in MTR units from fade entry."""
    bars = seg.bars[tf]
    tfns = E.TF_SEC[tf] * 1_000_000_000
    n = len(bars["t"])
    out = []
    for i in range(lookback + 1, n - 1):
        t_sig_close = bars["t"][i] + tfns
        if not (seg.t0 <= t_sig_close < seg.t1):
            continue
        o, c, h, l = bars["o"][i], bars["c"][i], bars["h"][i], bars["l"][i]
        body = abs(c - o)
        mb = E.median_body(bars, i, lookback)
        if mb <= 0 or body / mb < min_norm_body:
            continue
        m = E.mtr(bars, i, lookback)
        if m <= 0:
            continue
        bo_dir = 1 if c > o else (-1 if c < o else 0)
        if bo_dir == 0:
            continue
        px = h + buy_off if bo_dir > 0 else l - sell_off
        t_exp = bars["t"][i] + 2 * tfns
        i0 = np.searchsorted(seg.ts, t_sig_close, side="left")
        i_exp = np.searchsorted(seg.ts, t_exp, side="left")
        if bo_dir > 0:
            k = E.scan_first(seg.ask[i0:i_exp] >= px, 0, i_exp - i0)
        else:
            k = E.scan_first(seg.bid[i0:i_exp] <= px, 0, i_exp - i0)
        if k < 0:
            continue
        i_fill = i0 + k
        if max_spread is not None and (seg.ask[i_fill] - seg.bid[i_fill]) > max_spread:
            continue
        if hours is not None and pd.Timestamp(seg.ts[i_fill]).hour not in hours:
            continue
        if flow_gate is not None:
            v = seg.win_sum(seg.cum_v, i_fill + 1, MIN // 2)
            sv = seg.win_sum(seg.cum_sv, i_fill + 1, MIN // 2)
            aimb = (sv / v if v > 0 else 0.0) * bo_dir
            if aimb >= flow_gate:
                continue  # breakout has real flow -> don't fade
        d = -bo_dir  # fade direction
        fill = seg.bid[i_fill] if d < 0 else seg.ask[i_fill]
        sld = m * sl_mult
        tpd = sld * rr
        sl = fill - sld if d < 0 else fill + sld
        sl = fill + sld if d < 0 else fill - sld
        tp = fill - tpd if d < 0 else fill + tpd
        i_max = None
        if time_stop_h is not None:
            t_stop = seg.ts[i_fill] + int(time_stop_h * 3600 * 1e9)
            i_max = int(np.searchsorted(seg.ts, t_stop, side="left"))
        i_ex, px_ex, reason = E.sim_exit(seg, d, i_fill, sl, tp, i_max=i_max)
        pnl = (px_ex - fill) * d
        out.append({"sys": "CBF", "seg": seg.sym, "tf": tf, "dir": d,
                    "t_entry": seg.ts[i_fill], "entry": fill, "sl": sl, "tp": tp,
                    "t_exit": seg.ts[i_ex], "exit": px_ex, "reason": reason,
                    "pnl": pnl, "norm_body": body / mb, "mtr": m, "sld": sld,
                    "i_fill": i_fill})
    return out


def report(trades, cost=0.15, by=None):
    if not trades:
        print("  no trades"); return None
    df = pd.DataFrame(trades)
    df["pnl_net"] = df["pnl"] - cost
    s = E.summarize(trades, cost)
    print(f"  n={s['n']} pnl={s['pnl_pts']:.0f}pts avg={s['avg_pts']:.2f} "
          f"win={s['win%']:.0f}% pf={s['pf']:.2f} dd={s['maxdd_pts']:.0f}")
    if by:
        print(df.groupby(by).agg(n=('pnl_net', 'size'), tot=('pnl_net', 'sum'),
                                 avg=('pnl_net', 'mean')).round(2).to_string())
    return df
