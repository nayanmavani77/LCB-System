"""Adversarial audit of RETF for lookahead / leakage, using the exact
live-config shape (ATR stops, session window, rr 3, seed 42).

Checks:
 1. determinism        same tape twice -> byte-identical trade lists
 2. future-blindness   perturb all ticks after cutoff by +500 -> every
                       entry decided before cutoff is byte-identical
 3. prefix property    replay a truncated tape -> identical to the same
                       period of the full-tape run (nothing from the
                       future changes the past)
 4. fill honesty       long fills >= trade price (never better than the
                       market), tp exits pay exactly the tp, sl exits are
                       never profitable (breakeven off), and every entry
                       is inside the session window
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import random as _rnd

import pandas as pd

from core.broker import PaperBroker
from engines.retf import RETFStrategy

NS = 1_000_000_000
CFG = {"tf_min": 15, "sl_mode": "atr", "sl_mult": 1.0, "vol_window": 20,
       "rr": 3.0, "ema_period": 40,       # scaled-down EMA so the synthetic
       "entry_prob": 1.0, "seed": 42,     # tape warms up; logic identical
       "session_start": "19:00", "session_end": "20:59",
       "max_spread": 0.9, "sl_points": 5.0}


def make_tape(days=6, seed=7):
    """Synthetic GC-like tape: 1 tick/15s, 24h/day, mild trend + noise."""
    r = _rnd.Random(seed)
    t0 = pd.Timestamp("2026-03-02", tz="UTC").value
    px = 3000.0
    out = []
    n = days * 24 * 60 * 4                # 4 ticks/minute
    for i in range(n):
        px += r.gauss(0.02, 0.6)          # drift + noise
        out.append((t0 + i * 15 * NS, round(px, 2)))
    return out


def run(ticks, cfg=None, spy_entries=None):
    b = PaperBroker(trade_log_path=None, cost_pts=0.2, log=lambda *a: None)
    s = RETFStrategy(b, config=dict(cfg or CFG), log=lambda *a: None)
    if spy_entries is not None:
        orig = b.market_order
        def spy(ts, d, q, sl, tp, tag, ref_px=None):
            spy_entries.append((ts, d, round(sl, 6), round(tp, 6), tag,
                                round(ref_px, 6)))
            orig(ts, d, q, sl, tp, tag, ref_px=ref_px)
        b.market_order = spy
    for ts, px in ticks:
        b.on_tick(ts, px - 0.10, px + 0.10)      # replay order: broker first
        s.on_tick(ts, px, 1.0, "N", px - 0.10, px + 0.10)
    return b


def key(rec):
    return (rec["ts_open"], rec["ts_close"], rec["dir"], rec["entry"],
            rec["exit"], rec["sl"], rec["tp"], rec["reason"], rec["pnl_pts"])


tape = make_tape()

# 1 -- determinism
a = [key(r) for r in run(tape).closed]
b = [key(r) for r in run(tape).closed]
assert a and a == b, "NOT deterministic"
print(f"1. determinism      OK   ({len(a)} trades, identical twice)")

# 2 -- future-blindness (perturb everything after the midpoint)
cut = tape[len(tape) // 2][0]
e_base, e_pert = [], []
run(tape, spy_entries=e_base)
run([(ts, px + (500.0 if ts >= cut else 0.0)) for ts, px in tape],
    spy_entries=e_pert)
pre_b = [e for e in e_base if e[0] < cut]
pre_p = [e for e in e_pert if e[0] < cut]
assert pre_b and pre_b == pre_p, "LOOKAHEAD: past entries changed!"
print(f"2. future-blind     OK   ({len(pre_b)} pre-cutoff entries identical "
      f"under +500pt future shock)")

# 3 -- prefix property: truncated tape reproduces the full run's past
cut_i = int(len(tape) * 0.6)
full = [key(r) for r in run(tape).closed if r["ts_close"] < tape[cut_i][0]]
trunc = [key(r) for r in run(tape[:cut_i]).closed
         if r["ts_close"] < tape[cut_i][0]]
assert full == trunc, "PREFIX VIOLATION: future ticks changed closed past trades"
print(f"3. prefix property  OK   ({len(full)} closed trades identical with/"
      f"without the future 40% of the tape)")

# 4 -- fill honesty + session confinement
bk = run(tape)
px_at = dict(tape)
bad = []
for r in bk.closed:
    trade_px = px_at.get(r["ts_open"])
    if trade_px is not None:
        if r["dir"] > 0 and r["entry"] < trade_px - 1e-9:
            bad.append(("long filled BETTER than traded price", r))
        if r["dir"] < 0 and r["entry"] > trade_px + 1e-9:
            bad.append(("short filled BETTER than traded price", r))
    if r["reason"] == "tp":
        want = abs(r["tp"] - r["entry"]) - 0.2
        if abs(r["pnl_pts"] - (want if r["pnl_pts"] > 0 else want)) > 1e-6 \
           and abs(abs(r["exit"] - r["entry"]) - abs(r["tp"] - r["entry"])) > 1e-9:
            bad.append(("tp paid more than tp distance", r))
    if r["reason"] == "sl" and r["pnl_pts"] > 0:
        bad.append(("profitable 'sl' exit (stale-quote artifact)", r))
    minute = (r["ts_open"] // (60 * NS)) % 1440
    if not (19 * 60 <= minute < 20 * 60 + 59):
        bad.append(("entry outside 19:00-20:59 session", r))
assert not bad, bad[:3]
n_tp = sum(1 for r in bk.closed if r["reason"] == "tp")
n_sl = sum(1 for r in bk.closed if r["reason"] == "sl")
print(f"4. fill honesty     OK   ({len(bk.closed)} trades: {n_tp} tp / "
      f"{n_sl} sl; no better-than-market fills, no profitable 'sl', "
      f"all entries in session)")

print("\nAUDIT PASSED")
