"""Live runner - streams real-time ticks through the EXACT same engine that the backtest uses (engines/ + core/).

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
Broker implementation (see core/broker.py) - safe to leave running.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.broker import PaperBroker
from engines import ENGINES, Bar, TF_SECONDS


def get_api_key():
    """Databento key: config.py (DATABENTO_API_KEY = "db-...") wins,
    else the DATABENTO_API_KEY environment variable."""
    try:
        import config
        key = getattr(config, "DATABENTO_API_KEY", "") or ""
        if key.startswith("db-"):
            return key
    except ImportError:
        pass
    key = os.environ.get("DATABENTO_API_KEY", "")
    if not key.startswith("db-"):
        raise SystemExit(
            "No Databento API key found.\n"
            "Either copy config.example.py to config.py and paste your key there,\n"
            "or set the DATABENTO_API_KEY environment variable.")
    return key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-flow-gate", action="store_true")
    ap.add_argument("--broker", choices=["paper", "mt5"], default="paper")
    ap.add_argument("--engine", default="lrev", choices=["lrev", "ldef"],
                    help="strategy engine from engines/ (lrev = validated "
                         "BREAK engine; ldef = experimental DEFEND engine)")
    ap.add_argument("--mt5-symbol", default="XAUUSD")
    ap.add_argument("--lots", type=float, default=0.01)
    ap.add_argument("--trades-csv", default=None,
                    help="paper trade log (default: paper_trades_<SYMBOL>.csv)")
    ap.add_argument("--cost", type=float, default=0.4,
                    help="paper-mode commission+slippage per round turn (points)")
    ap.add_argument("--state-json", default=None,
                    help="state snapshot (default: lrev_state_<SYMBOL>.json)")
    from core.cli import add_strategy_args, config_from_args, describe
    from core.paths import log_path
    add_strategy_args(ap)
    args = ap.parse_args()

    import databento as db

    from core.symbols import get_symbol
    sym = get_symbol(args.symbol)
    mt5_symbol = args.mt5_symbol or sym["mt5_symbol"]
    if args.trades_csv is None:
        args.trades_csv = f"paper_trades_{sym['name']}.csv"
    if args.state_json is None:
        args.state_json = f"state_{sym['name']}.json"
    args.trades_csv = log_path(args.trades_csv)
    args.state_json = log_path(args.state_json)
    api_key = get_api_key()
    if args.broker == "mt5":
        from core.mt5_broker import MT5Broker
        print(f"note: {sym['mt5_lot_note']}")
        broker = MT5Broker(symbol=mt5_symbol, lots=args.lots,
                           signal_symbol=sym["name"])
    else:
        broker = PaperBroker(trade_log_path=args.trades_csv,
                             cost_pts=args.cost,
                             point_value=sym["point_value"])
    cfg = config_from_args(args, base={"use_flow_gate": not args.no_flow_gate})
    if args.max_spread is None:
        cfg["max_spread"] = sym["max_spread"]   # per-symbol default gate
    if args.engine == "ldef":
        from engines.ldef import DEFEND_CONFIG
        if args.flow_lo is None:
            cfg["flow_lo"] = DEFEND_CONFIG["flow_lo"]
        if args.flow_hi is None:
            cfg["flow_hi"] = DEFEND_CONFIG["flow_hi"]
        cfg["engine_name"] = "L-Def"
    strat = ENGINES[args.engine](broker, config=cfg)
    print("strategy:", describe(cfg))

    hist = db.Historical(api_key)
    end = pd.Timestamp.utcnow().floor("min") - pd.Timedelta(minutes=10)
    m1 = None
    for backoff_h in (0, 6, 24, 48):   # historical can lag real time
        try:
            e = end - pd.Timedelta(hours=backoff_h)
            start = e - pd.Timedelta(days=20)
            print(f"bootstrapping bars {start} .. {e} ...")
            data = hist.timeseries.get_range(
                dataset=sym["dataset"], schema="ohlcv-1m",
                stype_in="continuous", symbols=[sym["continuous"]],
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

    mode = ("PAPER trading" if args.broker == "paper"
            else f"LIVE orders -> MT5 {mt5_symbol} @ {args.lots} lots")
    print(f"subscribing to live TBBO ({sym['continuous']})... {mode}, Ctrl-C to stop")
    client = db.Live(key=api_key)
    client.subscribe(dataset=sym["dataset"], schema="tbbo",
                     stype_in="continuous", symbols=[sym["continuous"]])
    import time as _time
    n_ticks = 0
    last_beat = _time.time()
    try:
        for rec in client:
            if not hasattr(rec, "price"):
                continue
            bid = rec.bid_px_00 / 1e9
            ask = rec.ask_px_00 / 1e9
            px = rec.price / 1e9
            broker.on_tick(rec.ts_recv, bid, ask)
            strat.on_tick(rec.ts_recv, px, rec.size, rec.side, bid, ask)
            n_ticks += 1
            now = _time.time()
            if now - last_beat >= 60:
                print(f"[heartbeat] {_time.strftime('%H:%M:%S UTC', _time.gmtime())} | "
                      f"{n_ticks:,} ticks so far | {sym['name']} {bid:.2f}/{ask:.2f} | "
                      f"flow {strat.flow.imbalance():+.2f} | "
                      f"{len(strat.levels)} levels armed")
                last_beat = now
    except KeyboardInterrupt:
        pass
    finally:
        strat.save_state(args.state_json)
        if args.broker == "paper":
            from core.report import print_report
            print_report(broker, title="LIVE PAPER SESSION RESULT")
        else:
            broker.shutdown()
            print("MT5 disconnected; open positions remain protected by "
                  "their server-side SL/TP.")
        print("state saved:", args.state_json)


if __name__ == "__main__":
    main()
