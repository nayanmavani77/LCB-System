"""Live runner - streams real-time ticks through the EXACT same engine that the backtest uses (engines/ + core/).

Signals are generated from the GC futures TBBO stream (Databento). Execution
is pluggable:

    python run/live.py                                       # paper fills (default)
    python run/live.py --broker mt5 --mt5-symbol XAUUSD --lots 0.01
                                                             # real/demo MT5 account
    python run/live.py --broker mt5 --symbols GC:XAUUSD+:0.01,SI:XAGUSD+:0.02
                                                             # MULTI-SYMBOL, one terminal:
                                                             # one child process per symbol,
                                                             # per-symbol MT5 symbol + lots
    python run/live.py --no-flow-gate                        # v2-ea config

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


class _Tee:
    """Mirror everything written to a stream into a log file as well."""
    def __init__(self, stream, fh):
        self._s, self._f = stream, fh

    def write(self, data):
        self._s.write(data)
        try:
            self._f.write(data)
            self._f.flush()
        except Exception:
            pass                     # never let logging kill the stream

    def flush(self):
        self._s.flush()
        try:
            self._f.flush()
        except Exception:
            pass


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
            "Either create config.py with your key (template: docs/COMMANDS.md, section 1),\n"
            "or set the DATABENTO_API_KEY environment variable.")
    return key


def _parse_specs(spec_str, default_mt5, default_lots):
    """Parse --symbols. Each item: NAME[:MT5SYMBOL[:LOTS]].
    e.g.  GC                      -> registry MT5 symbol, --lots
          GC:XAUUSD+              -> explicit MT5 symbol, --lots
          GC:XAUUSD+:0.01,SI:XAGUSD+:0.02   -> fully explicit, two symbols
    """
    from core.symbols import get_symbol
    specs = []
    for item in spec_str.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        name = parts[0].upper()
        sym = get_symbol(name)          # validates the symbol name
        mt5s = (parts[1] if len(parts) > 1 and parts[1]
                else (default_mt5 or sym["mt5_symbol"]))
        try:
            lots = (float(parts[2]) if len(parts) > 2 and parts[2]
                    else default_lots)
        except ValueError:
            raise SystemExit(f"bad lots in --symbols item '{item}' "
                             f"(format: NAME[:MT5SYMBOL[:LOTS]])")
        specs.append((name, mt5s, lots))
    if not specs:
        raise SystemExit("--symbols is empty")
    return specs


# options the supervisor sets per child - stripped from the passthrough args
_PER_CHILD = {"--symbol", "--symbols", "--mt5-symbol", "--lots"}


def _passthrough(argv):
    """argv minus the per-child options (kept: --broker, --rr, --engine, ...)."""
    out, i = [], 0
    while i < len(argv):
        key = argv[i].split("=", 1)[0]
        if key in _PER_CHILD:
            i += 1 if "=" in argv[i] else 2
            continue
        out.append(argv[i])
        i += 1
    return out


def _supervise(specs, extra):
    """One terminal, N symbols: run one child process per symbol, prefix
    every output line with [SYMBOL], restart a child if it dies, and stop
    everything on Ctrl-C. Child isolation means one symbol's crash or
    reconnect never interrupts the others' trading."""
    import subprocess
    import threading
    import time

    here = os.path.abspath(__file__)

    def pump(name, p):
        for line in p.stdout:
            print(f"[{name}] {line}", end="", flush=True)

    def start(name, mt5s, lots):
        cmd = [sys.executable, "-u", here, "--symbol", name,
               "--mt5-symbol", mt5s, "--lots", str(lots)] + extra
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=pump, args=(name, p), daemon=True).start()
        return p

    print(f"supervisor: {len(specs)} symbols in one terminal - Ctrl-C stops all")
    procs = {}
    for name, mt5s, lots in specs:
        procs[name] = start(name, mt5s, lots)
        print(f"[{name}] started (MT5 symbol {mt5s} @ {lots} lots)")
    try:
        while True:
            time.sleep(2)
            for name, mt5s, lots in specs:
                p = procs[name]
                if p.poll() is not None:
                    print(f"[{name}] exited with code {p.returncode}; "
                          f"restarting in 10s")
                    time.sleep(10)
                    procs[name] = start(name, mt5s, lots)
    except KeyboardInterrupt:
        # on Windows/Unix the Ctrl-C also reaches the children (same console
        # group), so they save state and disconnect MT5 cleanly themselves
        print("\nsupervisor: stopping all symbols...")
    finally:
        for name, p in procs.items():
            try:
                p.wait(timeout=20)
            except Exception:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    pass
        print("supervisor: all stopped")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-flow-gate", action="store_true")
    ap.add_argument("--broker", choices=["paper", "mt5"], default="paper")
    ap.add_argument("--engine", default="lrev", choices=sorted(ENGINES),
                    help="strategy engine from engines/__init__.py registry "
                         "(lrev = validated BREAK engine)")
    ap.add_argument("--symbols", "--symbol", dest="symbols", default="GC",
                    help="comma list, per-symbol MT5 symbol and lots optional: "
                         "GC:XAUUSD+:0.01,SI:XAGUSD+:0.02 "
                         "(defaults: core/symbols.py registry and --lots)")
    ap.add_argument("--mt5-symbol", default=None,
                    help="MT5 symbol override (default: registry, e.g. GC->XAUUSD)")
    ap.add_argument("--lots", type=float, default=0.01,
                    help="default lots (per-symbol override via --symbols)")
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

    specs = _parse_specs(args.symbols, args.mt5_symbol, args.lots)
    if len(specs) > 1:
        return _supervise(specs, _passthrough(sys.argv[1:]))
    name, mt5_symbol, lots = specs[0]
    args.lots = lots

    import databento as db

    from core.symbols import get_symbol
    sym = get_symbol(name)
    if args.trades_csv is None:
        args.trades_csv = f"paper_trades_{sym['name']}.csv"
    if args.state_json is None:
        args.state_json = f"state_{sym['name']}.json"
    args.trades_csv = log_path(args.trades_csv)
    args.state_json = log_path(args.state_json)

    # mirror the whole session (heartbeats, levels, gates, fills, errors)
    # into logs/live_<SYMBOL>_<start time>.log
    from datetime import datetime, timezone
    session_log = log_path(
        f"live_{sym['name']}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.log")
    _log_fh = open(session_log, "a", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, _log_fh)
    sys.stderr = _Tee(sys.stderr, _log_fh)
    print(f"session log: {session_log}")

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
    strategy_cls = ENGINES[args.engine]
    base = {"use_flow_gate": not args.no_flow_gate}
    base.update(getattr(strategy_cls, "CLI_DEFAULTS", {}))
    cfg = config_from_args(args, base=base)
    if args.max_spread is None:
        cfg["max_spread"] = sym["max_spread"]   # per-symbol default gate
    strat = strategy_cls(broker, config=cfg)
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
    import time as _time
    RECONNECT_WAITS = (5, 15, 60, 300)   # escalating; stays at 5 min
    n_ticks = 0
    reconnects = 0
    stop = False
    try:
        while not stop:
            client = db.Live(key=api_key)
            client.subscribe(dataset=sym["dataset"], schema="tbbo",
                             stype_in="continuous", symbols=[sym["continuous"]])
            last_beat = _time.time()
            fresh_connection = True
            try:
                for rec in client:
                    if not hasattr(rec, "price"):
                        continue
                    if fresh_connection:
                        fresh_connection = False
                        if reconnects:
                            print(f"[reconnect] stream re-established, "
                                  f"resuming at tick {n_ticks:,}")
                        reconnects = 0   # healthy again -> reset backoff
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
                # iterator ended without an exception = gateway closed the session
                raise ConnectionError("live session closed by gateway")
            except KeyboardInterrupt:
                stop = True
            except Exception as exc:
                wait = RECONNECT_WAITS[min(reconnects, len(RECONNECT_WAITS) - 1)]
                reconnects += 1
                strat.save_state(args.state_json)
                print(f"[reconnect] stream lost: {exc}")
                print(f"[reconnect] state saved; retrying in {wait}s "
                      f"(attempt {reconnects}) - Ctrl-C to stop")
                try:
                    client.stop()
                except Exception:
                    pass
                try:
                    _time.sleep(wait)
                except KeyboardInterrupt:
                    stop = True
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
