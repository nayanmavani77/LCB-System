"""Live runner - executes the EXACT same engine (lrev/) that the backtest uses.

    export DATABENTO_API_KEY=db-XXXX
    python3 live.py                    # paper trading on real-time TBBO
    python3 live.py --no-flow-gate     # v2-ea config

Bootstraps warmup bars from Databento historical, then streams real-time
GC.v.0 TBBO. Fills are simulated by PaperBroker until you pass a real
Broker implementation (see lrev/broker.py) - safe to leave running.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from lrev import Bar, LRevStrategy, PaperBroker, TF_SECONDS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-flow-gate", action="store_true")
    ap.add_argument("--trades-csv", default="paper_trades.csv")
    ap.add_argument("--state-json", default="lrev_state.json")
    args = ap.parse_args()

    import databento as db

    broker = PaperBroker(trade_log_path=args.trades_csv)
    strat = LRevStrategy(broker, config={"use_flow_gate": not args.no_flow_gate})

    hist = db.Historical()
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
        from lrev.report import print_report
        print_report(broker, title="LIVE PAPER SESSION RESULT")
        print("state saved:", args.state_json)


if __name__ == "__main__":
    main()
