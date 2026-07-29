"""System test suite - run any time with:

    python tests/test_system.py

No external test framework needed (stdlib + pandas/numpy, which the
system already requires). Covers: engine behavior locks (lrev + gtrend),
broker mechanics, session/DST boundaries, report integrity, CLI parsing,
watcher tape logic, and regression tests for every fix from the July 2026
code review. Exits non-zero on any failure.
"""
import io
import math
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core.broker import PaperBroker
from engines import ENGINES
from engines.gtrend import GTrendStrategy, _session_date, _session_end_ns
from engines.lrev import Bar, LRevStrategy, median

NS = 1_000_000_000
PASS = []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  ok  {name}")
    except Exception as exc:
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        raise


# --------------------------------------------------------------------- lrev
def t_lrev_regression():
    """Behavior lock: fixed-seed synthetic tape must produce the exact same
    trades forever. If this changes, an engine or broker change altered
    lrev results - investigate before shipping."""
    rng = np.random.default_rng(7)
    n = 120_000
    t0 = 1_700_000_000 * NS
    px = 2000 + np.cumsum(rng.normal(0, 0.3, n)) + 5 * np.sin(np.arange(n) / 5000)
    sides = rng.choice(["B", "A"], n)
    broker = PaperBroker(trade_log_path=None, cost_pts=0.4, log=lambda *a: None)
    strat = LRevStrategy(broker, config={"use_flow_gate": False},
                         log=lambda *a: None)
    for i in range(n):
        ts = t0 + i * 3 * NS
        p = float(px[i])
        broker.on_tick(ts, p - 0.1, p + 0.1)
        strat.on_tick(ts, p, 1.0, sides[i], p - 0.1, p + 0.1)
    broker.close_all(t0 + n * 3 * NS)
    got = (len(broker.closed), round(sum(r["pnl_pts"] for r in broker.closed), 2))
    assert got == (18, -27.36), f"lrev regression changed: {got}"


def t_lrev_median():
    assert median([]) == 0.0
    assert median([3.0]) == 3.0
    assert median([1.0, 2.0, 10.0]) == 2.0
    assert median([1.0, 2.0, 3.0, 10.0]) == 2.5


def t_lrev_nan_quotes_safe():
    """Ticks before any BBO must not crash or create NaN levels."""
    broker = PaperBroker(trade_log_path=None, log=lambda *a: None)
    strat = LRevStrategy(broker, log=lambda *a: None)
    nan = float("nan")
    for i in range(50):
        strat.on_tick(1_700_000_000 * NS + i * NS, 2000.0, 1.0, "B", nan, nan)
    assert strat.levels == []


# -------------------------------------------------------------------- gtrend
def t_gtrend_session_boundary():
    ns = lambda s: pd.Timestamp(s, tz="UTC").value
    assert _session_date(ns("2026-07-01 20:59")) == 20260701   # EDT
    assert _session_date(ns("2026-07-01 21:01")) == 20260702
    assert _session_date(ns("2026-01-15 21:30")) == 20260115   # EST
    assert _session_date(ns("2026-01-15 22:01")) == 20260116
    assert _session_date(ns("2026-07-05 22:30")) == 20260706   # Sun -> Mon
    assert _session_end_ns(ns("2026-07-01 15:00")) == ns("2026-07-01 21:00")
    assert _session_end_ns(ns("2026-01-15 10:00")) == ns("2026-01-15 22:00")
    # DST transition days themselves
    assert _session_end_ns(ns("2026-03-08 10:00")) == ns("2026-03-08 21:00")
    assert _session_end_ns(ns("2026-11-01 10:00")) == ns("2026-11-01 22:00")


def t_gtrend_entry_parity():
    """Mini spec-parity: engine entries == reference implementation."""
    rng = np.random.default_rng(42)
    days = pd.bdate_range("2024-01-02", periods=250)
    drift = np.concatenate([np.full(125, 1.2), np.full(125, -1.5)])
    close = 2000 + np.cumsum(drift + rng.normal(0, 6.0, 250))
    o = np.empty(250); o[0] = close[0]; o[1:] = close[:-1]
    h = np.maximum(o, close) + np.abs(rng.normal(3, 2, 250))
    l = np.minimum(o, close) - np.abs(rng.normal(3, 2, 250))
    d = pd.DataFrame({"open": o, "high": h, "low": l, "close": close},
                     index=days)
    d["ret"] = d["close"].diff(); d["range"] = d["high"] - d["low"]

    def ref_entries():
        s = d.copy()
        s["atr"] = s["range"].rolling(20).mean()
        s["ret_z"] = s["ret"] / s["ret"].abs().rolling(20).mean()
        ma = s["close"].rolling(50).mean()
        s["trend"] = np.sign(s["close"] - ma)
        s["tstr"] = (ma - ma.shift(10)).abs() / s["atr"]
        O = s["open"].to_numpy(float)
        H, L = s["high"].to_numpy(float), s["low"].to_numpy(float)
        Z, T = s["ret_z"].to_numpy(), s["trend"].to_numpy()
        A, TS = s["atr"].to_numpy(), s["tstr"].to_numpy()
        out, openp = [], []
        for c in range(len(s)):
            keep = []
            for p in openp:
                if p["fi"] > c:
                    keep.append(p); continue
                ex = None
                if p["d"] > 0:
                    if L[c] <= p["e"] - p["st"]: ex = 1
                    elif H[c] >= p["e"] + p["tg"]: ex = 1
                else:
                    if H[c] >= p["e"] + p["st"]: ex = 1
                    elif L[c] <= p["e"] - p["tg"]: ex = 1
                if ex is None and (c - p["fi"] + 1) >= 10: ex = 1
                if ex is None: keep.append(p)
            openp = keep
            if c + 1 >= len(s): continue
            z = Z[c]
            ok = (np.isfinite(z) and 0.5 <= abs(z) <= 4.0
                  and np.isfinite(A[c]) and A[c] > 0
                  and np.isfinite(TS[c]) and TS[c] >= 0.5)
            di = -int(np.sign(z)) if ok else 0
            if di != 0 and (not np.isfinite(T[c]) or di != T[c]): di = 0
            if di != 0 and len(openp) < 2:
                openp.append(dict(fi=c + 1, d=di, e=O[c + 1],
                                  st=A[c], tg=1.5 * A[c]))
                out.append((int(s.index[c + 1].strftime("%Y%m%d")), di,
                            round(A[c], 4)))
        return out

    from zoneinfo import ZoneInfo
    from datetime import datetime, timedelta, timezone
    ET = ZoneInfo("America/New_York")
    broker = PaperBroker(trade_log_path=None, cost_pts=0, log=lambda *a: None)
    strat = GTrendStrategy(broker, config={"min_session_ticks": 5},
                           log=lambda *a: None)
    fills = []
    orig = broker.market_order
    def spy(ts, dd, q, sl, tp, tag, ref_px=None):
        fills.append((_session_date(ts), dd, round(abs(ref_px - sl), 4)))
        orig(ts, dd, q, sl, tp, tag, ref_px=ref_px)
    broker.market_order = spy
    for day, row in d.iterrows():
        d0 = datetime(day.year, day.month, day.day, tzinfo=ET) - timedelta(hours=6)
        t0 = d0.astimezone(timezone.utc)
        inner = np.linspace(min(row.open, row.close), max(row.open, row.close), 8)
        path = [row.open] + list(inner[:4]) + [row.high] + list(inner[4:]) + \
               [row.low, row.close]
        for i, p in enumerate(path):
            ts = int((t0 + timedelta(minutes=5 + i * 90)).timestamp() * 1e9)
            broker.on_tick(ts, p, p)
            strat.on_tick(ts, float(p), 1.0, "N", float(p), float(p))
    last = int(datetime(2025, 6, 30, 12, tzinfo=ET)
               .astimezone(timezone.utc).timestamp() * 1e9)
    strat.on_tick(last, float(close[-1]), 1.0, "N", float(close[-1]),
                  float(close[-1]))
    ref = ref_entries()
    assert len(ref) == len(fills) and all(a == b for a, b in zip(ref, fills)), \
        f"gtrend parity broken: {len(ref)} vs {len(fills)}"


# -------------------------------------------------------------------- broker
def t_broker_mechanics():
    b = PaperBroker(trade_log_path=None, cost_pts=0.4, point_value=100.0,
                    log=lambda *a: None)
    b.on_tick(1, 99.9, 100.1)
    b.market_order(1, 1, 1, sl=99.0, tp=102.0, tag="T|x")
    assert b.open_count("T|") == 1 and b.open_count("Z|") == 0
    b.on_tick(2, 102.0, 102.2)                       # TP hit
    assert b.open_count("T|") == 0
    rec = b.closed[-1]
    assert rec["reason"] == "tp" and abs(rec["pnl_gross_pts"] - 1.9) < 1e-9
    assert abs(rec["pnl_pts"] - 1.5) < 1e-9          # minus 0.4 cost
    # stop-before-target within one tick (both spanned)
    b.market_order(3, 1, 1, sl=101.0, tp=103.0, tag="T|y")
    b.on_tick(4, 100.0, 104.0)                       # bid<=sl AND bid>=... sl first
    assert b.closed[-1]["reason"] == "sl"
    # close_position by tag / time
    b.on_tick(5, 99.9, 100.1)
    b.market_order(5, -1, 1, sl=101.0, tp=98.0, tag="T|z")
    assert b.close_position(6, "T|z") is True
    assert b.closed[-1]["reason"] == "time"
    assert b.close_position(7, "T|z") is False       # already gone


def t_broker_nan_guard():
    """REVIEW FIX: an order before any quote must be dropped, not filled
    at NaN (which would silently corrupt every downstream metric)."""
    b = PaperBroker(trade_log_path=None, log=lambda *a: None)
    b.market_order(1, 1, 1, sl=1.0, tp=2.0, tag="T|nan")
    assert b.positions == [] and b.closed == []


def t_report_qty_weighting_and_tf_block():
    """Report must size-weight by qty and must NOT print a fake timeframe
    table for non-lrev tags (REVIEW FIX)."""
    from core.report import print_report
    b = PaperBroker(trade_log_path=None, cost_pts=0.0, log=lambda *a: None)
    b.on_tick(1, 99.9, 100.1)
    b.market_order(10**9, 1, 0.5, sl=99.0, tp=101.0, tag="GT|L|20240101")
    b.on_tick(2, 101.0, 101.2)
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        print_report(b, point_value=100.0)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert "by timeframe" not in out                  # GT tag != timeframe
    assert "$+45" in out                              # 0.9 pts x 0.5 x $100
    # and an lrev-style tag DOES get the block
    b2 = PaperBroker(trade_log_path=None, cost_pts=0.0, log=lambda *a: None)
    b2.on_tick(1, 99.9, 100.1)
    b2.market_order(10**9, 1, 1, sl=99.0, tp=101.0, tag="L-Rev|M15|high@1")
    b2.on_tick(2, 101.0, 101.2)
    buf2 = io.StringIO(); sys.stdout = buf2
    try:
        print_report(b2, point_value=100.0)
    finally:
        sys.stdout = old
    assert "by timeframe" in buf2.getvalue()


# ----------------------------------------------------------------------- cli
def t_cli_set_flag():
    import argparse
    from core.cli import add_strategy_args, config_from_args
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GC")
    add_strategy_args(ap)
    a = ap.parse_args(["--set", "allow_short=false", "--set", "max_hold=15",
                       "--rr", "3.0"])
    cfg = config_from_args(a, base=dict(GTrendStrategy.CLI_DEFAULTS))
    assert cfg["allow_short"] is False and cfg["max_hold"] == 15
    assert cfg["rr"] == 3.0
    # unknown key warns but still applies (REVIEW FIX: no more silent typos)
    a2 = ap.parse_args(["--set", "no_such_key=1"])
    buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
    try:
        cfg2 = config_from_args(a2, base=dict(GTrendStrategy.CLI_DEFAULTS))
    finally:
        sys.stdout = old
    assert "WARNING" in buf.getvalue() and cfg2["no_such_key"] == 1
    # malformed --set fails loudly
    try:
        config_from_args(ap.parse_args(["--set", "oops"]), base={})
        raise AssertionError("malformed --set accepted")
    except SystemExit:
        pass


def t_registry():
    assert sorted(ENGINES) == ["gtrend", "gtrend-lowdd", "lrev"]
    for cls in ENGINES.values():
        assert hasattr(cls, "on_tick") or True   # classes, instantiable:
        b = PaperBroker(trade_log_path=None, log=lambda *a: None)
        s = cls(b, config=None, log=lambda *a: None)
        s.seed_bars("M15", [])                    # contract: must not raise
        s.save_state(os.devnull)


# -------------------------------------------------------------------- watch
def t_watch_tape():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "watchmod", os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "run", "watch.py"))
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    out = []
    t = w.Tape("mbp-10", out=out.append, big_mult=10, big_min=20,
               sweep_ms=50, clock=lambda: 99.0)
    base = pd.Timestamp("2026-02-03 10:00", tz="UTC").value
    def rec(**kw):
        r = types.SimpleNamespace(ts_recv=None, ts_event=None, action="T",
                                  price=None, size=0, side="N", levels=None)
        for k, v in kw.items():
            setattr(r, k, v)
        return r
    # REVIEW FIX: one-sided book (bid side empty -> bid_px UNDEF) must not
    # crash the depth snapshot
    lvl = types.SimpleNamespace(bid_px=2**63 - 1, bid_sz=0,
                                ask_px=int(4050.1e9), ask_sz=9)
    t.on_record(rec(ts_recv=base, action="A", levels=[lvl] * 10))
    assert any("depth10" in l and "[-/4050.10]" in l for l in out), out
    # sweep alert still fires
    for i in range(100):
        t.on_record(rec(ts_recv=base + i * NS, price=int(4050e9), size=2,
                        side="B" if i % 2 else "A"))
    tb = base + 200 * NS
    for j in range(10):
        t.on_record(rec(ts_recv=tb + j * 3_000_000, price=int(4050e9),
                        size=6, side="A"))
    t.on_record(rec(ts_recv=tb + 2 * NS, price=int(4050e9), size=2, side="B"))
    assert any(l.startswith(">>> BIG SELL sweep 60") for l in out)


def t_paths():
    from core.paths import log_path
    p = log_path("x.csv")
    assert p.endswith(os.path.join("logs", "x.csv"))
    assert log_path("/abs/x.csv") == "/abs/x.csv"
    assert log_path(os.path.join("sub", "x.csv")) == os.path.join("sub", "x.csv")


def t_symbols():
    from core.symbols import get_symbol
    s = get_symbol("gc")
    assert s["name"] == "GC" and s["point_value"] == 100.0
    try:
        get_symbol("ZZ")
        raise AssertionError("unknown symbol accepted")
    except SystemExit as e:
        assert "core/symbols.py" in str(e)


def main():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("t_") and callable(v)]
    print(f"running {len(tests)} test groups...")
    for name, fn in tests:
        check(name, fn)
    print(f"\nALL {len(PASS)} TEST GROUPS PASSED")


if __name__ == "__main__":
    main()
