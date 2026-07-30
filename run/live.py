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
from core.data import ns_index
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


def closed_bars(m1, tf_seconds):
    """Resample 1-minute bars to `tf_seconds`, keeping ONLY bars the data
    fully covers.

    Without this the last resampled bar is a PARTIAL bar (e.g. at 18:47 the
    M15 bin 18:45 holds two minutes). Seeding it does two kinds of damage:
    its tiny true range drags the ATR down, and the live BarBuilder then
    rebuilds the same 18:45 bar from ticks - so the warmup bar is counted
    twice. The first bin is dropped for the same reason at the other end.
    """
    if not len(m1):
        return m1
    rule = f"{tf_seconds // 60}min"
    bars = pd.DataFrame({
        "open": m1["open"].resample(rule).first(),
        "high": m1["high"].resample(rule).max(),
        "low": m1["low"].resample(rule).min(),
        "close": m1["close"].resample(rule).last(),
        "volume": m1["volume"].resample(rule).sum(),
    }).dropna(subset=["open"])
    tf_ns = tf_seconds * 1_000_000_000
    first_ns = int(m1.index[0].value)                    # first 1m bar opens
    last_ns = int(m1.index[-1].value) + 60_000_000_000   # last 1m bar closes
    bt = ns_index(bars.index)   # unit-safe: a us-resolution index would
    # otherwise compare 1000x too small and let every bar through
    return bars[(bt >= first_ns) & (bt + tf_ns <= last_ns)]


def quote_ok(bid, ask, price):
    """Reject impossible quotes. Databento encodes an undefined price as
    INT64_MAX, which becomes ~9.2e9 after the 1e-9 scaling - feeding that to
    the paper broker books fictional multi-billion-dollar fills, and feeding
    it to a spread gate silently disables the gate. Also catches crossed and
    absurdly wide books at halts/reopens."""
    if not (bid == bid and ask == ask and price == price):
        return False                                   # NaN
    if bid <= 0 or ask <= 0 or price <= 0:
        return False
    if ask < bid:
        return False                                   # crossed
    if (ask - bid) > 0.10 * ask:
        return False                                   # > 10% wide = broken
    # the TRADE price can be UNDEF while the book is fine (and vice versa),
    # so check it against the book too - generously, real prints do land
    # outside the top of book, but never at half or double it
    return 0.5 * bid <= price <= 2.0 * ask


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
    ap.add_argument("--cost", type=float, default=None,
                    help="paper-mode commission+slippage per round turn "
                         "(points; default: per-symbol from core/symbols.py)")
    ap.add_argument("--state-json", default=None,
                    help="state snapshot (default: logs/state_<SYMBOL>.json)")
    ap.add_argument("--mt5-max-spread", type=float, default=None,
                    help="spread cap in PRICE units on the traded MT5 symbol "
                         "(default: the same value as the signal-side "
                         "--max-spread; 0 disables). The engine's gate only "
                         "sees the futures book - this one sees what you pay.")
    ap.add_argument("--max-signal-age", type=float, default=120.0,
                    help="refuse to send an order when the signal tick is "
                         "older than this many seconds (default 120, 0 "
                         "disables). Protects against acting on a lagging "
                         "or replayed stream.")
    ap.add_argument("--stall-timeout", type=float, default=300.0,
                    help="force a reconnect when no tick arrives for this "
                         "many seconds during market hours (default 300, 0 "
                         "disables). A half-open socket otherwise blocks "
                         "forever with no error.")
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
    # config BEFORE the broker: the MT5 adapter needs the engine's qty,
    # spread cap and concurrency cap to build its own execution-side gates
    strategy_cls = ENGINES[args.engine]
    base = {"use_flow_gate": not args.no_flow_gate}
    base.update(getattr(strategy_cls, "CLI_DEFAULTS", {}))
    cfg = config_from_args(args, base=base)
    if args.max_spread is None:
        cfg["max_spread"] = sym["max_spread"]   # per-symbol default gate
    if args.broker == "mt5":
        from core.mt5_broker import MT5Broker
        print(f"note: {sym['mt5_lot_note']}")
        # The engine's own spread gate reads the GC futures book; the MT5
        # book is what actually costs money, so the broker gets its OWN
        # spread cap. Same for exposure: after a restart the engine starts
        # flat while MT5 may still hold a position, so the cap is enforced
        # against every MAGIC position the terminal reports.
        broker = MT5Broker(symbol=mt5_symbol, lots=args.lots,
                           signal_symbol=sym["name"],
                           signal_log_path=log_path(f"mt5_signals_{tag}.csv"),
                           max_spread=(args.mt5_max_spread
                                       if args.mt5_max_spread is not None
                                       else cfg.get("max_spread") or None),
                           max_positions=cfg.get("max_concurrent"),
                           min_qty=float(cfg.get("qty") or 1.0),
                           max_signal_age_s=args.max_signal_age)
    else:
        cost = args.cost if args.cost is not None else sym["cost_pts"]
        broker = PaperBroker(trade_log_path=args.trades_csv,
                             cost_pts=cost,
                             point_value=sym["point_value"])
    strat = strategy_cls(broker, config=cfg)
    print("strategy:", strategy_cls.describe(cfg)
          if hasattr(strategy_cls, "describe") else describe(cfg))

    # engines declare how much history they need (default 20 calendar days;
    # e.g. G-Trend needs ~170 for its 50-session MA + slope + z windows)
    warmup_days = getattr(strategy_cls, "WARMUP_DAYS", 20)
    hist = db.Historical(api_key)
    end = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=10)
    m1 = None
    # try `end` first; when Databento rejects it, its error names the exact
    # available end ("data available up to '...'") - retry with THAT instead
    # of blindly backing off hours (a 6h hole in the bar history skews the
    # first session's ATR/EMA). The fixed backoffs remain as the fallback.
    attempts = [end] + [end - pd.Timedelta(hours=h) for h in (6, 24, 48)]
    tried = set()
    while attempts:
        e = attempts.pop(0)
        if e in tried:
            continue
        tried.add(e)
        try:
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
            import re as _re
            m = _re.search(r"available up to '([^']+)'", str(exc))
            if m:
                try:
                    avail = pd.Timestamp(m.group(1))
                    avail = (avail.tz_localize("UTC") if avail.tz is None
                             else avail.tz_convert("UTC")).floor("min")
                    if avail < e and avail not in tried:
                        attempts.insert(0, avail)   # retry at the exact edge
                except (ValueError, TypeError):
                    pass
            print(f"  historical not available yet ({exc}); backing off...")
    if m1 is None or not len(m1):
        print("WARNING: no warmup bars available - the engine will build bars "
              "from live ticks only (signals begin once enough bars form).")
        m1 = pd.DataFrame()
    # Which RAW contract(s) the continuous warmup series is stitched from.
    # A roll INSIDE the warmup window puts one artificial price jump (the
    # basis between the two contracts) into the bar history, which inflates
    # the true-range window and therefore an ATR stop. The backtest never
    # sees this - it replays each contract segment separately - so this is a
    # live-only distortion and worth saying out loud.
    if len(m1) and "symbol" in m1.columns:
        raws = list(dict.fromkeys(str(s) for s in m1["symbol"].dropna()))
        if raws:
            print(f"  warmup contract(s): {', '.join(raws)}")
        if len(raws) > 1:
            print(f"  WARNING: this {warmup_days}-day warmup window SPANS a "
                  f"contract roll ({raws[0]} -> {raws[-1]}). The stitched "
                  f"series holds one artificial price jump, so volatility "
                  f"windows (ATR/MTR) read wider than reality until "
                  f"~{cfg.get('vol_window', 20)} live bars have replaced it.")
    for tf, sec in TF_SECONDS.items():
        if not len(m1):
            break
        # closed_bars(): only bars the 1m data fully covers. Seeding the
        # PARTIAL last bin would both understate its true range and be
        # rebuilt a second time by the live BarBuilder from ticks.
        bars = closed_bars(m1, sec)
        strat.seed_bars(tf, [Bar(int(t), r.open, r.high, r.low, r.close, r.volume)
                             for t, r in zip(ns_index(bars.index),
                                             bars.itertuples())])
        print(f"  {tf}: {len(bars)} warmup bars (fully closed only)")

    mode = ("PAPER trading" if args.broker == "paper"
            else f"LIVE orders -> MT5 {mt5_symbol} @ {args.lots} lots")
    print(f"subscribing to live TBBO ({sym['continuous']})... {mode}, Ctrl-C to stop")
    import threading
    import time as _time
    RECONNECT_WAITS = (5, 15, 60, 300)   # escalating; stays at 5 min
    n_ticks = 0
    n_bad = 0
    reconnects = 0
    stop = False
    try:
        while not stop:
            client = db.Live(key=api_key)
            client.subscribe(dataset=sym["dataset"], schema="tbbo",
                             stype_in="continuous", symbols=[sym["continuous"]])
            last_beat = _time.time()
            fresh_connection = True
            seen = {"at": _time.time()}
            wd_stop = threading.Event()

            def _watchdog(c=client, st=seen, ev=wd_stop,
                          limit=float(args.stall_timeout or 0)):
                """A half-open socket makes `for rec in client` block
                FOREVER: no exception, no heartbeat, and the last line
                printed still looks healthy - you only find out when a
                session produced no trades. So watch the clock from the
                side and force the reconnect path."""
                warned = 0.0
                while not ev.wait(5.0):
                    idle = _time.time() - st["at"]
                    if idle >= limit:
                        print(f"[stall] no ticks for {idle:.0f}s "
                              f"(limit {limit:.0f}s) - forcing a reconnect")
                        try:
                            c.stop()
                        except Exception:
                            pass
                        return
                    if idle >= limit / 2 and idle - warned >= limit / 2:
                        warned = idle
                        print(f"[idle] no ticks for {idle:.0f}s "
                              f"(reconnect at {limit:.0f}s; normal when the "
                              f"market is closed)")

            if args.stall_timeout and args.stall_timeout > 0:
                threading.Thread(target=_watchdog, daemon=True).start()
            try:
                for rec in client:
                    seen["at"] = _time.time()
                    # Databento announces which RAW contract the continuous
                    # symbol currently points at (volume-based: highest
                    # volume on the PREVIOUS trading day, so around rolls it
                    # can lag the crossover by a day - expect thin quotes)
                    if isinstance(rec, db.SymbolMappingMsg):
                        raw = (getattr(rec, "stype_out_symbol", "")
                               or getattr(rec, "stype_in_symbol", "?"))
                        print(f"[contract] {sym['continuous']} -> {raw} "
                              f"(front month by prior-day volume)")
                        continue
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
                    # Databento encodes an UNDEFINED price as INT64_MAX,
                    # which becomes ~9.2e9 here. Unfiltered it books
                    # fictional paper fills and makes every spread gate pass
                    # (a 9-billion-wide book is not <= 0.9, but a 9-billion
                    # ASK against a real bid is), so drop it at the door.
                    if not quote_ok(bid, ask, px):
                        n_bad += 1
                        if n_bad <= 3 or n_bad % 1000 == 0:
                            print(f"[quote] dropped invalid tick #{n_bad}: "
                                  f"px={px:.6g} bid={bid:.6g} ask={ask:.6g}")
                        continue
                    broker.on_tick(rec.ts_recv, bid, ask)
                    strat.on_tick(rec.ts_recv, px, rec.size, rec.side, bid, ask)
                    n_ticks += 1
                    now = _time.time()
                    if now - last_beat >= 60:
                        status = (strat.status() if hasattr(strat, "status")
                                  else f"flow {strat.flow.imbalance():+.2f} | "
                                       f"{len(strat.levels)} levels armed")
                        lag = max(0.0, now - rec.ts_recv / 1e9)
                        print(f"[heartbeat] {_time.strftime('%H:%M:%S UTC', _time.gmtime())} | "
                              f"{n_ticks:,} ticks so far | {sym['name']} "
                              f"{bid:.2f}/{ask:.2f} | lag {lag:.1f}s"
                              f"{f' | {n_bad} bad quotes dropped' if n_bad else ''}"
                              f" | {status}")
                        last_beat = now
                # iterator ended without an exception = gateway closed the
                # session (this is also how a watchdog stall lands here)
                raise ConnectionError("live session closed by gateway")
            except KeyboardInterrupt:
                wd_stop.set()
                stop = True
            except Exception as exc:
                wd_stop.set()
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
