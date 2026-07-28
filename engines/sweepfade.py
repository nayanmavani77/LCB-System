"""Sweep-Fade: fade BIG sweeps (spec: docs/SWEEPFADE_SPEC.md).

A BIG sweep - one aggressor walking through multiple price levels in
milliseconds - is a liquidity artefact, not price discovery. Market makers
re-quote around pre-sweep fair value within seconds and price retraces.
This engine trades AGAINST every qualifying sweep and holds at most 10
minutes. Spec's 30h GC sample: +0.355 R/trade, 54% win, PF 1.81, t=+2.52
(inverse control -0.335 R). SMALL SAMPLE (98 trades, 2 sessions, both
volatile/down-trending) - the spec itself calls it a hypothesis, not a
validated system. Backtest across regimes before any live use.

RULES (decided on the first tick AFTER a sweep cluster closes - the same
moment run/watch.py would print the >>> BIG line; no look-ahead):
  BIG detection  identical to run/watch.py: consecutive same-side fills
                 within sweep_ms cluster into one order; BIG when the sum
                 >= max(big_min, big_mult x EMA avg trade size).
  HARD GATE      kind must be a SWEEP (n_prints > 1). Single big PRINTS
                 continue - never trade them (spec S5.1).
  QUALITY        qscore = (x_avg>=18 or size>=35)      # magnitude
                        + (n_prints >= 6)              # multilevel
                        + (no opposite-side BIG in prior 5 min)   # clean
                 trade only if qscore >= 2 (NOT >=3 - unstable, spec S5.3)
  DIRECTION      fade: BUY sweep -> SHORT, SELL sweep -> LONG
  ENTRY          market immediately (every delay/limit variant tested
                 worse - adverse selection, spec S6.1)
  STOP           clamp(0.5 x trailing-300s trade-price range, 8, 45) ticks
  TARGET         2.5 x stop distance (resting limit)
  TIME STOP      flatten after 600 s - a PRIMARY exit (43% of trades)
  ONE POSITION   at a time; NO cooldown; NO breakeven/trailing/partial
                 exits (all tested worse, spec S7.3/S12 - do not add them)

Sizing: fixed qty/--lots per trade. The spec's risk-percent sizing (S7.2)
and session limits (S7.4) are NOT implemented - size externally. qscore
is recorded in each trade's tag (SWF|L|q2|17) for later analysis.

Cost note for backtests: PaperBroker already charges the quoted spread
inside its bid/ask fills and gap-through stops, which covers the spec's
entry/exit slippage economics; --cost adds commission+extra slip per round
turn ON TOP. The per-symbol default (0.4 pts = 4 ticks on GC) is the
conservative house model; --cost 0.05 (~$5 commission only) approximates
the spec's cost table. Break-even is ~5-6 ticks of entry slippage - this
is the strategy's single point of failure (spec S8).

Ticks: stop maths use cfg["tick"] (GC 0.10). For another instrument set
--set tick=... AND re-derive every threshold - they are instrument-specific.
"""
from __future__ import annotations

import collections
import json

SWEEPFADE_CONFIG = {
    # ---- BIG detection (keep identical to run/watch.py defaults) ----
    "big_mult": 10.0,          # BIG if size >= this x EMA avg trade size...
    "big_min": 20.0,           # ...and always at least this many contracts
    "sweep_ms": 50.0,          # same-side fills within this = one sweep
    # ---- signal quality (spec S5.2) ----
    "mag_x_avg": 18.0,
    "mag_size": 35.0,
    "min_prints": 6,
    "clean_window_s": 300.0,
    "min_qscore": 2,
    # ---- risk (spec S6) ----
    "tick": 0.10,              # GC tick size (price units)
    "range_window_s": 300.0,   # trailing trade-price window for the stop
    "stop_range_mult": 0.50,
    "stop_min_ticks": 8.0,
    "stop_max_ticks": 45.0,
    "target_r": 2.5,
    "max_hold_s": 600.0,       # time stop - primary exit
    # ---- housekeeping ----
    "max_concurrent": 1,       # spec S7.1: one position at a time
    "max_spread": 0.9,         # entry spread gate ($; per-symbol default)
    "qty": 1,
    "engine_name": "Sweep-Fade",
    "tag_prefix": "SWF",
}


class SweepFadeStrategy:
    """Tick-stream implementation: sweep clustering runs inline (same
    algorithm as run/watch.py), the signal is evaluated when a cluster
    closes (= the first tick after the sweep), entry is a market order on
    that same tick. Exits: broker SL/TP (stop checked first on both
    brokers) + engine-driven 600s time stop via broker.close_position."""

    WARMUP_DAYS = 1
    CONFIG = SWEEPFADE_CONFIG
    CLI_DEFAULTS = dict(SWEEPFADE_CONFIG)

    def __init__(self, broker, config: dict | None = None, log=print):
        cfg = dict(type(self).CONFIG)
        if config:
            cfg.update(config)     # runner cfg carries L-Rev keys; ignored
        self.cfg = cfg
        self.broker = broker
        self.log = log
        self.now = 0
        self.bid = self.ask = float("nan")
        # rolling trade-price window for the volatility stop
        self._px_win = collections.deque()          # (ts, price)
        # BIG-event history for c_clean: (ts, side) - both kinds
        self._bigs = collections.deque()
        # sweep cluster (same fields as run/watch.py)
        self.avg_size = None
        self._cl = None
        # open-trade tracking for the time stop
        self._open = []                              # [{tag, ts}]
        self._n_events = 0
        self._n_signals = 0
        self._last_big = None

    # ---------------------------------------------------------------- seeding
    def seed_bars(self, tf, bars):
        """No bar warmup needed - state builds from the live tape."""

    # ---------------------------------------------------------------- events
    def on_tick(self, ts: int, price: float, size: float, side: str,
                bid: float, ask: float):
        self.now, self.bid, self.ask = ts, bid, ask
        cfg = self.cfg

        # 1) time stop - primary exit (43% of trades), checked every tick
        if self._open:
            keep = []
            for t in self._open:
                if self.broker.open_count(t["tag"]) == 0:
                    continue                          # closed by SL/TP
                if ts - t["ts"] >= cfg["max_hold_s"] * 1e9:
                    self.log(f"[Sweep-Fade] TIME STOP after "
                             f"{(ts - t['ts']) / 60e9:.1f} min [{t['tag']}]")
                    if not self.broker.close_position(ts, t["tag"]):
                        keep.append(t)                # retry next tick
                else:
                    keep.append(t)
            self._open = keep

        # 2) rolling price window (trade prints only)
        win = self._px_win
        win.append((ts, price))
        lo = ts - int(cfg["range_window_s"] * 1e9)
        while win and win[0][0] < lo:
            win.popleft()

        # 3) sweep clustering - identical flow to run/watch.py
        if self._cl is not None and (side != self._cl["side"]
                                     or ts - self._cl["t1"]
                                     > cfg["sweep_ms"] * 1e6):
            self._flush_cluster()
        if side in ("B", "A"):
            if self._cl is None:
                self._cl = dict(side=side, t0=ts, t1=ts, size=0.0, n=0,
                                px0=price, px1=price)
            self._cl["t1"] = ts
            self._cl["px1"] = price
            self._cl["size"] += size
            self._cl["n"] += 1
        self.avg_size = (size if self.avg_size is None
                         else self.avg_size * 0.99 + size * 0.01)

    # ---------------------------------------------------------------- signal
    def _flush_cluster(self):
        cfg = self.cfg
        cl, self._cl = self._cl, None
        if cl is None:
            return
        thr = max(cfg["big_min"], (self.avg_size or 0.0) * cfg["big_mult"])
        if cl["size"] < thr:
            return
        # ---- a BIG event exists (print or sweep) ----
        self._n_events += 1
        x_avg = cl["size"] / self.avg_size if self.avg_size else 0.0
        # c_clean is strictly backward-looking: evaluate BEFORE recording
        lo = self.now - int(cfg["clean_window_s"] * 1e9)
        opposite = sum(1 for t, s in self._bigs
                       if t >= lo and s != cl["side"])
        self._bigs.append((self.now, cl["side"]))
        while self._bigs and self._bigs[0][0] < lo:
            self._bigs.popleft()
        self._last_big = dict(side=cl["side"], size=cl["size"], n=cl["n"],
                              x=x_avg)

        if cl["n"] <= 1:
            return                     # hard gate: PRINTS are never traded
        c_mag = x_avg >= cfg["mag_x_avg"] or cl["size"] >= cfg["mag_size"]
        c_multi = cl["n"] >= cfg["min_prints"]
        c_clean = opposite == 0
        qscore = int(c_mag) + int(c_multi) + int(c_clean)
        if qscore < cfg["min_qscore"]:
            return
        if self.broker.open_count(cfg["tag_prefix"] + "|") \
                >= cfg["max_concurrent"]:
            return                     # one position at a time (no queue)
        spread = self.ask - self.bid
        if cfg["max_spread"] > 0 and spread == spread \
                and spread > cfg["max_spread"]:
            self.log(f"[Sweep-Fade] SKIPPED: spread {spread:.2f} > "
                     f"{cfg['max_spread']}")
            return
        if len(self._px_win) < 2:
            return                     # no volatility estimate yet
        prices = [p for _, p in self._px_win]
        rng_ticks = (max(prices) - min(prices)) / cfg["tick"]
        stop_ticks = min(max(cfg["stop_range_mult"] * rng_ticks,
                             cfg["stop_min_ticks"]), cfg["stop_max_ticks"])
        stop_dist = stop_ticks * cfg["tick"]

        direction = -1 if cl["side"] == "B" else 1      # FADE the sweep
        ref = self._px_win[-1][1]                       # current trade price
        sl = ref - direction * stop_dist
        tp = ref + direction * cfg["target_r"] * stop_dist
        self._n_signals += 1
        tag = (f"{cfg['tag_prefix']}|{'L' if direction > 0 else 'S'}|"
               f"q{qscore}|{self._n_events}")
        self.log(f"[Sweep-Fade] {'LONG' if direction > 0 else 'SHORT'} vs "
                 f"BIG {cl['side']} sweep {cl['size']:.0f} (n={cl['n']}, "
                 f"{x_avg:.0f}x, q{qscore}) | stop {stop_ticks:.0f}t "
                 f"SL {sl:.2f} TP {tp:.2f}")
        self.broker.market_order(self.now, direction, cfg["qty"],
                                 sl, tp, tag, ref_px=ref)
        self._open.append(dict(tag=tag, ts=self.now))

    # ---------------------------------------------------------------- misc
    def status(self) -> str:
        cfg = self.cfg
        n_open = self.broker.open_count(cfg["tag_prefix"] + "|")
        lb = self._last_big
        last = (f"last BIG {lb['side']} {lb['size']:.0f} n={lb['n']}"
                if lb else "no BIG yet")
        return (f"{last} | {self._n_events} BIGs, {self._n_signals} trades"
                f" | {n_open}/{cfg['max_concurrent']} open")

    @staticmethod
    def describe(cfg) -> str:
        return (f"{cfg.get('engine_name', 'Sweep-Fade')} | FADE big sweeps "
                f">= max({cfg['big_min']:.0f}, {cfg['big_mult']:.0f}x avg) "
                f"| qscore>={cfg['min_qscore']} "
                f"(mag {cfg['mag_x_avg']:.0f}x/{cfg['mag_size']:.0f}, "
                f"n>={cfg['min_prints']}, clean {cfg['clean_window_s']:.0f}s)"
                f" | stop clamp({cfg['stop_range_mult']}x300s-range, "
                f"{cfg['stop_min_ticks']:.0f}, {cfg['stop_max_ticks']:.0f})t"
                f" | TP {cfg['target_r']}R | hold<={cfg['max_hold_s']:.0f}s"
                f" | max {cfg['max_concurrent']} open")

    def save_state(self, path):
        with open(path, "w") as f:
            json.dump(dict(engine=self.cfg["engine_name"],
                           events=self._n_events, signals=self._n_signals,
                           open=self._open, last_big=self._last_big), f,
                      indent=1, default=str)
