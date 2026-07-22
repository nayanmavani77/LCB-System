"""Run the L-Rev v2 engine: replay on your DBN-derived cache, or live on Databento.

Replay (works today, no subscription needed - uses data_cache/ from prep.py):
    python3 run_live.py --mode replay --start 2026-06-01 --end 2026-07-17
    python3 run_live.py --mode replay --no-flow-gate        # EA-only config

Live paper trading (needs DATABENTO_API_KEY with GLBX.MDP3 live access):
    python3 run_live.py --mode live

Live mode bootstraps warmup bars from Databento historical, then streams
real-time TBBO. All fills are simulated by PaperBroker until you implement
a real Broker adapter (see broker.py) - so it is safe to leave running.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from broker import PaperBroker
from strategy import Bar, LRevStrategy, TF_SECONDS

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.environ.get("LCB_CACHE", os.path.join(_REPO, "data_cache"))


def quiet(*a, **k):
    pass


# ---------------------------------------------------------------- replay
def replay(args):
    import json

    import numpy as np

    with open(os.path.join(CACHE, "segments.json")) as f:
        segments = json.load(f)

    t0 = pd.Timestamp(args.start, tz="UTC").value if args.start else 0
    t1 = pd.Timestamp(args.end, tz="UTC").value if args.end else 2**63 - 1

    log = print if args.verbose else quiet
    broker = PaperBroker(trade_log_path=args.trades_csv, log=log)
    total = 0
    for seg in segments:
        seg_t0 = pd.Timestamp(seg["start"], tz="UTC").value
        seg_t1 = pd.Timestamp(seg["end"], tz="UTC").value
        if seg_t1 < t0 or seg_t0 > t1:
            continue
        sym = seg["symbol"]
        strat = LRevStrategy(broker, config={
            "use_flow_gate": not args.no_flow_gate}, log=log)
        # warmup bars from the cached per-contract bar files
        for tf in TF_SECONDS:
            bars = pd.read_parquet(os.path.join(CACHE, "seg", f"{sym}_{tf}.parquet"))
            bt = bars.index.view("int64")
            pre = bars[(bt < max(seg_t0, t0))]
            strat.seed_bars(tf, [Bar(int(t), r.open, r.high, r.low, r.close, r.volume)
                                 for t, r in zip(pre.index.view("int64"),
                                                 pre.itertuples())])
        tb = pd.read_parquet(os.path.join(CACHE, "seg", f"{sym}_tbbo.parquet"))
        tb = tb[(tb.ts >= max(seg_t0, t0)) & (tb.ts < min(seg_t1, t1))]
        n = len(tb)
        print(f"replaying {sym}: {n:,} ticks "
              f"({pd.Timestamp(seg['start'])} .. {pd.Timestamp(seg['end'])})")
        side_map = {ord('B'): 'B', ord('A'): 'A', ord('N'): 'N'}
        ts_a = tb.ts.to_numpy()
        px_a = tb.price.to_numpy()
        sz_a = tb['size'].to_numpy()
        sd_a = tb.side.to_numpy()
        bid_a = tb.bid.to_numpy()
        ask_a = tb.ask.to_numpy()
        for i in range(n):
            broker.on_tick(int(ts_a[i]), bid_a[i], ask_a[i])
            strat.on_tick(int(ts_a[i]), px_a[i], sz_a[i],
                          side_map.get(sd_a[i], 'N'), bid_a[i], ask_a[i])
        # contract roll: flatten and reset (same as backtest segments)
        broker.close_all(int(ts_a[-1]) if n else seg_t1)
        total += n
    print(f"\nreplayed {total:,} ticks")
    print("summary:", broker.summary())
    if args.trades_csv:
        print("trade log:", args.trades_csv)


# ---------------------------------------------------------------- live
def live(args):
    import databento as db

    broker = PaperBroker(trade_log_path=args.trades_csv)
    strat = LRevStrategy(broker, config={"use_flow_gate": not args.no_flow_gate})

    # bootstrap warmup bars from historical ohlcv-1m (last ~20 days)
    hist = db.Historical()  # DATABENTO_API_KEY
    end = pd.Timestamp.utcnow().floor("min") - pd.Timedelta(minutes=10)
    start = end - pd.Timedelta(days=20)
    print(f"bootstrapping bars {start} .. {end} ...")
    data = hist.timeseries.get_range(
        dataset="GLBX.MDP3", schema="ohlcv-1m",
        stype_in="continuous", symbols=["GC.v.0"],
        start=start.isoformat(), end=end.isoformat())
    m1 = data.to_df()
    for tf, sec in TF_SECONDS.items():
        rule = f"{sec // 60}min"
        bars = pd.DataFrame({
            "open": m1["open"].resample(rule).first(),
            "high": m1["high"].resample(rule).max(),
            "low": m1["low"].resample(rule).min(),
            "close": m1["close"].resample(rule).last(),
            "volume": m1["volume"].resample(rule).sum(),
        }).dropna(subset=["open"])
        strat.seed_bars(tf, [Bar(int(t), r.open, r.high, r.low, r.close, r.volume)
                             for t, r in zip(bars.index.view("int64"),
                                             bars.itertuples())])
        print(f"  {tf}: {len(bars)} warmup bars")

    print("subscribing to live TBBO (GC.v.0)... paper trading, Ctrl-C to stop")
    client = db.Live()
    client.subscribe(dataset="GLBX.MDP3", schema="tbbo",
                     stype_in="continuous", symbols=["GC.v.0"])
    try:
        for rec in client:
            if not hasattr(rec, "price"):
                continue
            bid = rec.bid_px_00 / 1e9
            ask = rec.ask_px_00 / 1e9
            px = rec.price / 1e9
            broker.on_tick(rec.ts_recv, bid, ask)
            strat.on_tick(rec.ts_recv, px, rec.size, rec.side, bid, ask)
    except KeyboardInterrupt:
        pass
    finally:
        strat.save_state(args.state_json)
        print("\nsummary:", broker.summary())
        print("state saved:", args.state_json)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["replay", "live"], default="replay")
    ap.add_argument("--start", default=None, help="replay window start (UTC)")
    ap.add_argument("--end", default=None, help="replay window end (UTC)")
    ap.add_argument("--no-flow-gate", action="store_true",
                    help="disable the TBBO flow gate (EA-only config)")
    ap.add_argument("--trades-csv", default="paper_trades.csv")
    ap.add_argument("--state-json", default="lrev_state.json")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if os.path.exists(args.trades_csv):
        os.remove(args.trades_csv)
    if args.mode == "replay":
        replay(args)
    else:
        live(args)


if __name__ == "__main__":
    main()
