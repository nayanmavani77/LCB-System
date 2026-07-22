"""Run the LCB backtest for any date window.

Usage:
    python3 run_backtest.py --start 2026-04-01 --end 2026-07-19 --config v2-flow
    python3 run_backtest.py --start 2026-02-01 --end 2026-03-15 --config v2-ea --csv out.csv

Configs:
    v1-l      original L-System (no gates)
    v1-cb     original CB-System
    v2-ea     v2 gates implementable in the EA alone (age cap 35h + spread cap 0.90)
    v2-flow   v2-ea plus the TBBO flow gate [0.0, 0.6]   (the PRIMARY strategy)

Notes:
    - Data available: 2025-12-28 .. 2026-07-17 (front-month GC via TBBO).
      Requested dates outside this range are clamped automatically.
    - A trade belongs to the window if its ENTRY time falls inside it.
      Signal/level history before the window is still used for warmup,
      exactly as a live EA would have it.
    - P&L is net of real quoted spread at fill + 0.15 pts/round turn
      (commission + slippage), 1 contract, GC = $100/point.
"""
import argparse
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import numpy as np
import engine as E
import variants as V

# derive available window from the segment table (extends automatically with new data)
DATA_START = pd.Timestamp(min(b[0] for b in E.SEG_BOUNDS.values()), tz="UTC")
DATA_END = pd.Timestamp(max(b[1] for b in E.SEG_BOUNDS.values()), tz="UTC")
ALLOWED_ALL = (("1", "-1"), ("1", "1"), ("-1", "-1"), ("-1", "1"), ("1", "0"), ("-1", "0"))
COST = 0.15


def run_config(segs, config):
    trades = []
    for sname, seg in segs.items():
        for tf, slm in [("M15", 1.5), ("H1", 0.5), ("H4", 0.5)]:
            if config == "v1-l":
                trades += E.run_l(seg, tf, slm, 2.0, log_features=False)
            elif config == "v1-cb":
                trades += E.run_cb(seg, tf, slm, 2.0)
            elif config == "v2-ea":
                trades += V.run_l_rev(seg, tf, slm, 2.0, allowed=ALLOWED_ALL,
                                      max_wait_h=35, max_spread=0.9)
            elif config == "v2-flow":
                trades += V.run_l_rev(seg, tf, slm, 2.0, allowed=ALLOWED_ALL,
                                      max_wait_h=35, max_spread=0.9,
                                      flow_lo=0.0, flow_hi=0.6)
            else:
                raise SystemExit(f"unknown config: {config}")
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--config", default="v2-flow",
                    choices=["v1-l", "v1-cb", "v2-ea", "v2-flow"])
    ap.add_argument("--csv", default=None, help="optional: save trades to CSV")
    args = ap.parse_args()

    t0 = max(pd.Timestamp(args.start, tz="UTC"), DATA_START)
    t1 = min(pd.Timestamp(args.end, tz="UTC"), DATA_END)
    if t1 <= t0:
        raise SystemExit("end must be after start (data: 2025-12-28..2026-07-17)")
    if pd.Timestamp(args.start, tz="UTC") < DATA_START or pd.Timestamp(args.end, tz="UTC") > DATA_END:
        print(f"note: window clamped to available data -> {t0.date()} .. {t1.date()}")

    segs = {s: E.Seg(s) for s in E.SEGMENTS}
    trades = run_config(segs, args.config)

    df = pd.DataFrame(trades)
    df["dtn"] = pd.to_datetime(df["t_entry"], utc=True)
    df = df[(df["dtn"] >= t0) & (df["dtn"] < t1)].sort_values("t_entry").reset_index(drop=True)
    if df.empty:
        print("no trades in this window")
        return
    df["pnl_net"] = df["pnl"] - COST

    eq = df["pnl_net"].cumsum()
    pf = df.pnl_net[df.pnl_net > 0].sum() / max(1e-9, -df.pnl_net[df.pnl_net < 0].sum())
    print(f"\n== {args.config} | {t0.date()} .. {t1.date()} ==")
    print(f"trades        : {len(df)}")
    print(f"net P&L       : {df.pnl_net.sum():+.1f} pts  (${df.pnl_net.sum()*100:+,.0f} per contract)")
    print(f"avg / trade   : {df.pnl_net.mean():+.2f} pts")
    print(f"win rate      : {100*(df.pnl_net>0).mean():.0f}%")
    print(f"profit factor : {pf:.2f}")
    print(f"max drawdown  : {(eq.cummax()-eq).max():.0f} pts")
    print("\nmonthly:")
    print(df.groupby(df["dtn"].dt.tz_convert(None).dt.to_period("M"))["pnl_net"]
            .agg(n="size", pts="sum").round(1).to_string())
    print("\nby timeframe:")
    print(df.groupby("tf")["pnl_net"].agg(n="size", pts="sum", avg="mean").round(2).to_string())

    if args.csv:
        out = df.copy()
        out["entry_time"] = out["dtn"].astype(str)
        out["exit_time"] = pd.to_datetime(out["t_exit"], utc=True).astype(str)
        keep = [c for c in ["entry_time", "exit_time", "seg", "tf", "dir", "level",
                            "entry", "sl", "tp", "exit", "reason", "pnl_net", "wait_h"]
                if c in out.columns]
        out[keep].round(4).to_csv(args.csv, index=False)
        print(f"\nsaved {len(out)} trades -> {args.csv}")


if __name__ == "__main__":
    main()
