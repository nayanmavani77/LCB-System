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
    """Parse --symbols. Each item: NAME[:MT5SYMBOL[:LOTS[:ENGINE]]].
    e.g.  GC                          -> registry MT5 symbol, --lots, --engine
          GC:XAUUSD+                  -> explicit MT5 symbol, --lots
          GC:XAUUSD+:0.01,SI:XAGUSD+:0.02            -> two symbols
          GC:XAUUSD+:0.01:lrev,GC:XAUUSD+:0.01:gtrend -> two ENGINES, one symbol
    """
    from core.symbols import get_symbol
    from engines import ENGINES
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
                             f"(format: NAME[:MT5SYMBOL[:LOTS[:ENGINE]]])")
        eng = parts[3].lower() if len(parts) > 3 and parts[3] else None
        if eng is not None and eng not in ENGINES:
            raise SystemExit(f"unknown engine '{eng}' in --symbols item "
                             f"'{item}' (valid: {sorted(ENGINES)})")
        specs.append((name, mt5s, lots, eng))
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
    """One terminal, N children: one child process per (symbol, engine)
    entry, every output line prefixed with [SYMBOL] or [SYMBOL/engine],
    a child that dies is restarted, Ctrl-C stops all. Child isolation
    means one entry's crash or reconnect never interrupts the others."""
    import subprocess
    import threading
    import time

    here = os.path.abspath(__file__)

    def label_of(name, eng):
        return f"{name}/{eng}" if eng else name

    def pump(label, p):
        for line in p.stdout:
            print(f"[{label}] {line}", end="", flush=True)

    def start(name, mt5s, lots, eng):
        # per-child options come AFTER the shared extras, so they win
        # (argparse: the last occurrence of an option takes effect)
        cmd = [sys.executable, "-u", here] + extra + \
              ["--symbol", name, "--mt5-symbol", mt5s, "--lots", str(lots)]
        if eng:
            cmd += ["--engine", eng]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=pump, args=(label_of(name, eng), p),
                         daemon=True).start()
        return p

    labels = [label_of(n, e) for n, _, _, e in specs]
    if len(set(labels)) != len(labels):
        raise SystemExit("duplicate --symbols entries (same symbol AND same "
                         "engine twice makes no sense - give each entry a "
                         "different engine via NAME:MT5:LOTS:ENGINE)")
    print(f"supervisor: {len(specs)} children in one terminal - Ctrl-C stops all")
    procs = {}
    for name, mt5s, lots, eng in specs:
        lab = label_of(name, eng)
        procs[lab] = start(name, mt5s, lots, eng)
        print(f"[{lab}] started (MT5 symbol {mt5s} @ {lots} lots"
              f"{', engine ' + eng if eng else ''})")
    try:
        while True:
            time.sleep(2)
            for name, mt5s, lots, eng in specs:
                lab = label_of(name, eng)
                p = procs[lab]
                if p.poll() is not None:
                    print(f"[{lab}] exited with code {p.returncode}; "
                          f"restarting in 10s")
                    time.sleep(10)
                    procs[lab] = start(name, mt5s, lots, eng)
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
    name, mt5_symbol, lots, spec_engine = specs[0]
    args.lots = lots
    if spec_engine:
        args.engine = spec_engine

    import databento as db

    from core.symbols import get_symbol
    sym = get_symbol(name)
    # engine-aware file names, so different engines on the SAME symbol
    # (parallel terminals or one supervisor) never overwrite each other;
    # lrev keeps the plain names for continuity
    tag = sym["name"] if args.engine == "lrev" else f"{sym['name']}_{args.engine}"
    if args.trades_csv is None:
        args.trades_csv = f"paper_trades_{tag}.csv"
    if args.state_json is None:
        args.state_json = f"state_{tag}.json"
    args.trades_csv = log_path(args.trades_csv)
    args.state_json = log_path(args.state_json)

    # mirror the whole session (heartbeats, levels, gates, fills, errors)
    # into logs/live_<SYMBOL>_<start time>.log
    from datetime import datetime, timezone
    session_log = log_path(
        f"live_{tag}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.log")
    _log_fh = open(session_log, "a", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, _log_fh)
    sys.stderr = _Tee(sys.stderr, _log_fh)
    print(f"session log: {session_log}")

    api_key = get_api_key()
    if args.broker == "mt5":
        from core.mt5_broker import MT5Broker
        print(f"note: {sym['mt5_lot_note']}")
        broker = MT5Broker(symbol=mt5_symbol, lots=args.lots,
                           signal_symbol=sym["name"],
                           signal_log_path=log_path(f"mt5_signals_{tag}.csv"))
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
    print("strategy:", strategy_cls.describe(cfg)
          if hasattr(strategy_cls, "describe") else describe(cfg))

    # engines declare how much history they need (default 20 calendar days;
    # e.g. G-Trend needs ~170 for its 50-session MA + slope + z windows)
    warmup_days = getattr(strategy_cls, "WARMUP_DAYS", 20)
    hist = db.Historical(api_key)
    end = pd.Timestamp.utcnow().floor("min") - pd.Timedelta(minutes=10)
    m1 = None
    for backoff_h in (0, 6, 24, 48):   # historical can lag real time
        try:
            e = end - pd.Timedelta(hours=backoff_h)
            start = e - pd.Timedelta(days=warmup_days)
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
                        status = (strat.status() if hasattr(strat, "status")
                                  else f"flow {strat.flow.imbalance():+.2f} | "
                                       f"{len(strat.levels)} levels armed")
                        print(f"[heartbeat] {_time.strftime('%H:%M:%S UTC', _time.gmtime())} | "
                              f"{n_ticks:,} ticks so far | {sym['name']} "
                              f"{bid:.2f}/{ask:.2f} | {status}")
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
