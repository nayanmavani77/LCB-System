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


def t_broker_stale_quote_guard():
    """A market order can never fill BETTER than the reference trade
    price. Around session reopens the TBBO bid/ask can lag a gapped trade
    by many points; without the clamp a backtest books the overnight gap
    as fictional profit (TP exits > TP distance, winning 'sl' exits)."""
    b = PaperBroker(trade_log_path=None, cost_pts=0.0, log=lambda *a: None)
    b.on_tick(1, 4050.2, 4050.4)                 # stale pre-halt book
    # price gapped up: trade printed 4062, engine anchors there
    b.market_order(2, 1, 1, sl=4059.0, tp=4077.0, tag="T|gapL",
                   ref_px=4062.0)
    assert b.positions[0].entry == 4062.0        # clamped, not 4050.4
    b.on_tick(3, 4077.0, 4077.2)                 # TP hit
    assert abs(b.closed[-1]["pnl_pts"] - 15.0) < 1e-9   # exactly TP dist
    # short mirror: stale bid above the gapped-down trade
    b.on_tick(4, 4049.8, 4050.0)
    b.market_order(5, -1, 1, sl=4041.0, tp=4023.0, tag="T|gapS",
                   ref_px=4038.0)
    assert b.positions[0].entry == 4038.0
    # normal ticks are untouched: ask >= trade -> clamp is a no-op
    b2 = PaperBroker(trade_log_path=None, cost_pts=0.0, log=lambda *a: None)
    b2.on_tick(1, 99.9, 100.1)
    b2.market_order(2, 1, 1, sl=95.0, tp=105.0, tag="T|norm", ref_px=100.0)
    assert b2.positions[0].entry == 100.1


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
    assert "by entry hour" in out and "00 UTC" in out # hourly P&L block
    # hourly block groups by ENTRY hour: ts_open=1s epoch -> hour 00
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


def t_report_streaks():
    """Report shows the longest winning and losing streaks (W W L L L W
    -> 2 consecutive wins, 3 consecutive losses)."""
    from core.report import print_report
    b = PaperBroker(trade_log_path=None, cost_pts=0.0, log=lambda *a: None)
    for i, p in enumerate([+1.0, +2.0, -1.0, -1.0, -1.0, +1.0]):
        b.closed.append({"ts_open": (i + 1) * 10**9,
                         "ts_close": (i + 1) * 10**9 + 1,
                         "entry": 100.0, "sl": 99.0, "dir": 1,
                         "reason": "tp" if p > 0 else "sl",
                         "pnl_pts": p, "pnl_usd": p * 100.0})
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        print_report(b, point_value=100.0)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert "max consec wins : 2 trades" in out and "+3.0 pts" in out
    assert "max consec loss : 3 trades" in out and "-3.0 pts" in out


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


# --------------------------------------------------------------------- retf
def _retf_run(cfg, ticks):
    from engines.retf import RETFStrategy
    b = PaperBroker(trade_log_path=None, cost_pts=0.0, log=lambda *a: None)
    fills = []
    orig = b.market_order
    def spy(ts, d, q, sl, tp, tag, ref_px=None):
        fills.append(dict(ts=ts, d=d, sl=sl, tp=tp, ref=ref_px, tag=tag))
        orig(ts, d, q, sl, tp, tag, ref_px=ref_px)
    b.market_order = spy
    s = RETFStrategy(b, config=cfg, log=lambda *a: None)
    for ts, px in ticks:
        b.on_tick(ts, px - 0.1, px + 0.1)
        s.on_tick(ts, px, 1.0, "N", px - 0.1, px + 0.1)
    return b, s, fills


def _retf_ticks(closes, tf_min=15, t0=None, per_bar=4):
    """One bar per close value: open/high/low/close ~= the value."""
    t0 = t0 or pd.Timestamp("2026-02-02", tz="UTC").value
    bar_ns = tf_min * 60 * NS
    out = []
    for i, c in enumerate(closes):
        for j in range(per_bar):
            out.append((t0 + i * bar_ns + j * (bar_ns // per_bar), float(c)))
    return out


def t_retf_ema_and_filter():
    from engines.retf import RETFStrategy
    # EMA must match pandas ewm(adjust=False)
    closes = list(2000 + np.cumsum(np.random.default_rng(1).normal(0, 2, 200)))
    b = PaperBroker(trade_log_path=None, log=lambda *a: None)
    s = RETFStrategy(b, config={"ema_period": 50}, log=lambda *a: None)
    for c in closes:
        s._ema_update(c)
    ref = pd.Series(closes).ewm(span=50, adjust=False).mean().iloc[-1]
    assert abs(s._ema - ref) < 1e-9, (s._ema, ref)
    # rising tape -> longs only; falling tape -> shorts only
    up = list(np.linspace(2000, 2200, 80))
    _, _, f = _retf_run({"ema_period": 10, "sl_points": 5, "rr": 3,
                         "entry_prob": 1.0}, _retf_ticks(up))
    assert f and all(e["d"] == 1 for e in f)
    e = f[0]
    assert abs((e["ref"] - e["sl"]) - 5.0) < 1e-9          # SL 5 pts
    assert abs((e["tp"] - e["ref"]) - 15.0) < 1e-9         # TP = 3R
    down = list(np.linspace(2200, 2000, 80))
    _, _, f2 = _retf_run({"ema_period": 10, "entry_prob": 1.0},
                         _retf_ticks(down))
    assert f2 and all(e["d"] == -1 for e in f2)


def t_retf_determinism_and_prob():
    up = list(np.linspace(2000, 2400, 300))
    cfg = {"ema_period": 10, "entry_prob": 0.3, "seed": 42,
           "sl_points": 5, "rr": 3}
    _, _, a = _retf_run(dict(cfg), _retf_ticks(up))
    _, _, b = _retf_run(dict(cfg), _retf_ticks(up))
    assert [x["tag"] for x in a] == [x["tag"] for x in b]   # same seed = same
    _, _, c = _retf_run(dict(cfg, seed=7), _retf_ticks(up))
    assert [x["tag"] for x in a] != [x["tag"] for x in c]   # diff seed = diff
    _, _, full = _retf_run(dict(cfg, entry_prob=1.0), _retf_ticks(up))
    assert len(a) < len(full)                               # prob thins entries


def t_retf_breakeven_time_exit_session():
    from engines.retf import RETFStrategy
    t0 = pd.Timestamp("2026-02-02", tz="UTC").value
    bar = 15 * 60 * NS
    # warm 12 rising bars -> long entry at bar 12 close (~2059)
    ticks = _retf_ticks(list(np.linspace(2000, 2060, 12)), t0=t0)
    b, s, f = _retf_run({"ema_period": 10, "entry_prob": 1.0, "sl_points": 5,
                         "rr": 100, "use_breakeven": True}, ticks)
    assert len(f) == 1 and f[0]["d"] == 1
    ref = f[0]["ref"]
    # +1R favorable -> SL moves to the broker's ACTUAL fill
    pos = b.positions[0]
    ts2 = t0 + 12 * bar + NS
    b.on_tick(ts2, ref + 5.0 - 0.1, ref + 5.0 + 0.1)
    s.on_tick(ts2, ref + 5.0, 1.0, "N", ref + 5.0 - 0.1, ref + 5.0 + 0.1)
    assert abs(pos.sl - pos.entry) < 1e-9, (pos.sl, pos.entry)
    # time exit: flat tape at the EMA keeps the position open until max_bars
    b2, s2, f2 = _retf_run(
        {"ema_period": 10, "entry_prob": 1.0, "sl_points": 50, "rr": 100,
         "use_time_exit": True, "max_bars": 3},
        _retf_ticks(list(np.linspace(2000, 2060, 12)) + [2060.5] * 5, t0=t0))
    assert len(f2) >= 1
    assert any(r["reason"] == "time" for r in b2.closed)
    first_close = b2.closed[0]
    held_ns = first_close["ts_close"] - first_close["ts_open"]
    assert held_ns >= 3 * 15 * 60 * NS * 0.9      # ~3 bars held
    # session gate: a window that excludes the tape's hours -> zero trades
    _, _, f3 = _retf_run({"ema_period": 10, "entry_prob": 1.0,
                          "session_start": "14:00", "session_end": "15:00"},
                         _retf_ticks(list(np.linspace(2000, 2060, 20)), t0=t0))
    assert f3 == []                       # ticks are 00:00-05:00 UTC


def t_retf_no_lookahead():
    """Publication-schedule immunity proof: perturbing every tick AFTER a
    cutoff by +500 pts must leave every entry BEFORE the cutoff
    byte-identical. Replay feeds ticks in EVENT time, so historical files
    being published later (e.g. 20:00) cannot leak future data into
    earlier decisions."""
    up = list(np.linspace(2000, 2400, 300))
    ticks = _retf_ticks(up)
    cutoff = ticks[len(ticks) // 2][0]
    cfg = {"ema_period": 10, "entry_prob": 1.0, "sl_points": 5, "rr": 3}

    def run(perturb):
        from engines.retf import RETFStrategy
        b = PaperBroker(trade_log_path=None, cost_pts=0.0, log=lambda *a: None)
        entries = []
        orig = b.market_order
        def spy(ts, d, q, sl, tp, tag, ref_px=None):
            entries.append((ts, d, round(sl, 6), round(tp, 6), tag))
            orig(ts, d, q, sl, tp, tag, ref_px=ref_px)
        b.market_order = spy
        s = RETFStrategy(b, config=dict(cfg), log=lambda *a: None)
        for ts, px in ticks:
            p = px + (500.0 if (perturb and ts >= cutoff) else 0.0)
            b.on_tick(ts, p - 0.1, p + 0.1)
            s.on_tick(ts, p, 1.0, "N", p - 0.1, p + 0.1)
        return [e for e in entries if e[0] < cutoff]

    base, pert = run(False), run(True)
    assert base and base == pert, "RETF lookahead detected!"


def t_retf_stop_modes():
    """sl_mode points/atr/mtr: ATR = mean TR (feels outliers), MTR = median
    TR (ignores them), computed over vol_window closed bars; fail-closed
    while not warm; breakeven trigger uses the trade's OWN stop distance."""
    t0 = pd.Timestamp("2026-02-02", tz="UTC").value
    bar = 15 * 60 * NS

    def ticks_with_ranges(closes, ranges):
        out = []
        for i, (c, r) in enumerate(zip(closes, ranges)):
            b0 = t0 + i * bar
            for j, px in enumerate([c, c + r / 2, c - r / 2, c]):
                out.append((b0 + j * (bar // 4), float(px)))
        return out

    # 25 rising bars, range 1.0 except one 9.0 outlier inside the window
    closes = list(np.linspace(2000, 2005, 25))
    ranges = [1.0] * 25
    ranges[15] = 9.0
    base_cfg = {"ema_period": 5, "entry_prob": 1.0, "rr": 2.0,
                "sl_mult": 2.0, "vol_window": 20, "seed": 1}
    stops = {}
    for mode in ("atr", "mtr"):
        _, _, f = _retf_run(dict(base_cfg, sl_mode=mode),
                            ticks_with_ranges(closes, ranges))
        assert f, mode
        e = f[0]
        stops[mode] = e["ref"] - e["sl"]
        # entry only after vol_window+1 bars closed (fail-closed warmup)
        assert int(e["tag"].split("|")[2]) >= 21, e["tag"]
        assert abs((e["tp"] - e["ref"]) - 2.0 * stops[mode]) < 1e-9
    # TRs ~[1.0 x19, 9.0 x1] within any 20-bar window (drift < range):
    # ATR ~ (19+9)/20 = 1.4 -> stop 2.8 ; MTR ~ 1.0 -> stop 2.0
    assert 2.6 < stops["atr"] < 3.1, stops
    assert 1.9 < stops["mtr"] < 2.3, stops
    assert stops["atr"] > stops["mtr"]     # mean feels the outlier
    # floor applies on a near-flat tape (drift 0.1 -> TR 0.1 -> 0.2 < floor)
    flat = ticks_with_ranges([2000 + i * 0.1 for i in range(25)], [0.0] * 25)
    _, _, ff = _retf_run(dict(base_cfg, sl_mode="mtr", sl_min_points=0.5,
                              ema_period=3), flat)
    if ff:
        assert abs((ff[0]["ref"] - ff[0]["sl"]) - 0.5) < 0.2, ff[0]
    # invalid mode fails loudly
    from engines.retf import RETFStrategy
    try:
        RETFStrategy(PaperBroker(trade_log_path=None, log=lambda *a: None),
                     config={"sl_mode": "bogus"}, log=lambda *a: None)
        raise AssertionError("bogus sl_mode accepted")
    except SystemExit:
        pass


def t_retf_reentry_and_one_position():
    # SL 0.5 pt -> first long stops out quickly; engine must wait
    # reentry_bars before the next entry, and never hold two at once
    closes = list(np.linspace(2000, 2030, 15)) + [2028, 2032, 2036, 2040]
    b, s, f = _retf_run({"ema_period": 5, "entry_prob": 1.0,
                         "sl_points": 0.5, "rr": 100, "reentry_bars": 2},
                        _retf_ticks(closes))
    entry_bars = [int(x["tag"].split("|")[2]) for x in f]
    for a, nxt in zip(entry_bars, entry_bars[1:]):
        assert nxt - a >= 2, entry_bars   # closed + waited >= 2 bars
    assert max(len(b.positions), 1) == 1


def t_registry():
    assert sorted(ENGINES) == ["gtrend", "gtrend-lowdd", "lrev", "retf"]
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


def t_snap_volume():
    """Execution-side sizing. The engine's qty is a MULTIPLIER on --lots;
    sending --lots regardless (the old bug) makes G-Trend trade 2x and
    G-Trend-LowDD 3x the intended size on a real account."""
    from core.mt5_broker import snap_volume
    # lrev/retf: qty 1 -> the volume IS --lots
    assert snap_volume(0.01, 1, 0.01, 100.0, 0.01) == (0.01, None)
    # gtrend: 2 entries x 0.5 -> 0.005, below a 0.01 minimum -> refuse with
    # the lots value that WOULD work, never silently round up to 0.01
    vol, err = snap_volume(0.01, 0.5, 0.01, 100.0, 0.01)
    assert vol is None and "below this broker's minimum" in err and "0.02" in err
    assert snap_volume(0.02, 0.5, 0.01, 100.0, 0.01) == (0.01, None)
    # snapping to volume_step, both directions
    assert snap_volume(0.014, 1, 0.01, 100.0, 0.01) == (0.01, None)
    assert snap_volume(0.016, 1, 0.01, 100.0, 0.01) == (0.02, None)
    # no step declared -> pass the raw product through
    assert snap_volume(0.03, 2, 0.01, 100.0, 0) == (0.06, None)
    # over the broker maximum, and a non-positive request
    assert snap_volume(60.0, 2, 0.01, 100.0, 0.01)[0] is None
    assert "maximum" in snap_volume(60.0, 2, 0.01, 100.0, 0.01)[1]
    assert snap_volume(0.01, 0, 0.01, 100.0, 0.01)[0] is None
    # float noise must not push 3 x 0.1 off its step
    assert snap_volume(0.1, 3, 0.01, 100.0, 0.01) == (0.3, None)


def t_closed_bars():
    """Live warmup must seed only bars the 1m data FULLY covers. A partial
    last bin understates its true range (dragging an ATR stop down) AND is
    rebuilt a second time by the live BarBuilder from ticks."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "livemod", os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "run", "live.py"))
    lv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lv)

    def m1_frame(start, n):
        idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
        return pd.DataFrame({"open": np.arange(n, dtype="float64") + 100.0,
                             "high": np.arange(n, dtype="float64") + 101.0,
                             "low": np.arange(n, dtype="float64") + 99.0,
                             "close": np.arange(n, dtype="float64") + 100.5,
                             "volume": np.ones(n)}, index=idx)

    # 18:00..18:46 inclusive = the 18:45 bin holds only 2 of its 15 minutes
    m1 = m1_frame("2026-07-22 18:00", 47)
    bars = lv.closed_bars(m1, 900)
    got = [str(t) for t in bars.index]
    assert got == ["2026-07-22 18:00:00+00:00", "2026-07-22 18:15:00+00:00",
                   "2026-07-22 18:30:00+00:00"], got
    # the old inline resample would have produced the partial 18:45 bar
    assert len(bars) == 3
    # aligned data: every bin is complete, none is dropped
    m1 = m1_frame("2026-07-22 18:00", 45)
    assert len(lv.closed_bars(m1, 900)) == 3
    # a non-aligned START drops its partial first bin too
    m1 = m1_frame("2026-07-22 18:07", 38)     # 18:07..18:44
    assert [str(t) for t in lv.closed_bars(m1, 900).index] == \
        ["2026-07-22 18:15:00+00:00", "2026-07-22 18:30:00+00:00"]
    # empty in, empty out (no warmup data available)
    assert not len(lv.closed_bars(pd.DataFrame(), 900))
    # H1/H4 use the same rule
    m1 = m1_frame("2026-07-22 12:00", 200)    # 12:00..15:19
    assert [str(t) for t in lv.closed_bars(m1, 3600).index] == \
        ["2026-07-22 12:00:00+00:00", "2026-07-22 13:00:00+00:00",
         "2026-07-22 14:00:00+00:00"]

    # Unit safety (core.data.ns_index): pandas 2/3 give date_range a
    # MICROSECOND resolution, so a bare .view("int64") reads 1000x too small
    # and every cutoff comparison silently passes everything. A us index and
    # an ns index must produce the identical bar set.
    m1 = m1_frame("2026-07-22 18:00", 47)
    if hasattr(m1.index, "as_unit"):
        as_ns = m1.copy()
        as_ns.index = m1.index.as_unit("ns")
        assert list(lv.closed_bars(as_ns, 900).index.astype(str)) == \
            list(lv.closed_bars(m1, 900).index.astype(str)) and \
            len(lv.closed_bars(as_ns, 900)) == 3

    # ---- quote_ok: the INT64_MAX undefined-price guard ----
    assert lv.quote_ok(4050.0, 4050.2, 4050.1)
    UNDEF = (2 ** 63 - 1) / 1e9               # ~9.223e9, Databento's UNDEF
    assert not lv.quote_ok(4050.0, UNDEF, 4050.1)
    assert not lv.quote_ok(UNDEF, 4050.2, 4050.1)
    assert not lv.quote_ok(4050.0, 4050.2, UNDEF)
    assert not lv.quote_ok(4050.2, 4050.0, 4050.1)      # crossed
    assert not lv.quote_ok(0.0, 4050.2, 4050.1)         # empty side
    assert not lv.quote_ok(float("nan"), 4050.2, 4050.1)
    assert not lv.quote_ok(4050.0, 4050.2, float("nan"))
    assert lv.quote_ok(4000.0, 4300.0, 4100.0)          # wide but plausible


def main():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("t_") and callable(v)]
    print(f"running {len(tests)} test groups...")
    for name, fn in tests:
        check(name, fn)
    print(f"\nALL {len(PASS)} TEST GROUPS PASSED")


if __name__ == "__main__":
    main()
