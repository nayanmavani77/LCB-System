"""Live runner - executes the EXACT same engine (lrev/) that the backtest uses.

Signals are generated from the GC futures TBBO stream (Databento). Execution
is pluggable:

    python3 live.py                                          # paper fills (default)
    python3 live.py --broker mt5 --mt5-symbol XAUUSD --lots 0.01
                                                             # real/demo MT5 account
    python3 live.py --no-flow-gate                           # v2-ea config

With --broker mt5, GC signal prices are translated to XAUUSD as SL/TP
DISTANCES re-anchored on the live XAUUSD quote (the futures/spot basis
cancels out). Test on a DEMO MT5 account first. Requires Windows,
`pip install MetaTrader5`, and the MT5 terminal running and logged in.

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
    ap.add_argument("--broker", choices=["paper", "mt5"], default="paper")
    ap.add_argument("--mt5-symbol", default="XAUUSD")
    ap.add_argument("--lots", type=float, default=0.01)
    ap.add_argument("--trades-csv", default="paper_trades.csv")
    ap.add_argument("--state-json", default="lrev_state.json")
    args = ap.parse_args()

    import databento as db

    if args.broker == "mt5":
        from lrev.mt5_broker import MT5Broker
        broker = MT5Broker(symbol=args.mt5_symbol, lots=args.lots)
    else:
        broker = PaperBroker(trade_log_path=args.trades_csv)
    strat = LRevStrategy(broker, config={"use_flow_gate": not args.no_flow_gate})

    hist = db.Historical()
    end = pd.Timestamp.utcnow().floor("min") - pd.Timedelta(minutes=10)
    m1 = None
    for backoff_h in (0, 6, 24, 48):   # historical can lag real time
        try:
            e = end - pd.Timedelta(hours=backoff_h)
            start = e - pd.Timedelta(days=20)
            print(f"bootstrapping bars {start} .. {e} ...")
            data = hist.timeseries.get_range(
                dataset="GLBX.MDP3", schema="ohlcv-1m",
                stype_in="continuous", symbols=["GC.v.0"],
                start=start.isoformat(), end=e.isoformat())
            m1 = data.to_df()
            if len(m1):
                break
        except Exception as exc:
            print(f"  historical not available yet ({exc}); backing off...")
    if m1 is None or not len(m1):
        print("WARNING: no warmup bars available - the engine will build bars "
              "from live ticks only (signals begin once enough bars form).")
        m1 = pd.DataFrame()
    for tf, sec in TF_SECONDS.items():
        if not len(m1):
            break
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
        if args.broker == "paper":
            from lrev.report import print_report
            print_report(broker, title="LIVE PAPER SESSION RESULT")
        else:
            broker.shutdown()
            print("MT5 disconnected; open positions remain protected by "
                  "their server-side SL/TP.")
        print("state saved:", args.state_json)


if __name__ == "__main__":
    main()
