"""RETF: Random Entry with Trend Filter.

Entries are RANDOM in timing; only the DIRECTION is constrained by an EMA
trend filter. One position at a time, continuous trading, fixed-size
SL/TP brackets. This is a baseline/benchmark engine: it measures how much
of a market's profitability comes from trend-beta + exit geometry alone.
A "real" strategy should beat RETF run with the same filter and costs -
if it doesn't, its entries add nothing.

RULES (evaluated at each closed bar of the trading timeframe):
  FILTER    EMA(ema_period) on bar closes. close > EMA -> only LONGS
            allowed; close < EMA -> only SHORTS. Optional stricter mode
            (require_slope=true): EMA must also slope in the trade
            direction (EMA now vs EMA slope_lag bars ago).
  ENTRY     if flat, inside the session window, >= reentry_bars since the
            last close, and a seeded coin-flip < entry_prob: market order
            at the bar-close tick in the filter's direction.
  EXITS     SL = entry -/+ sl_points; TP = entry +/- sl_points x rr.
            use_breakeven: at +1R favorable, SL moves to the ACTUAL fill.
            use_time_exit: flatten at market after max_bars bars.
  REENTRY   wait >= reentry_bars closed bars after a position closes.

Determinism: entries use random.Random(cfg["seed"]) - identical data and
seed reproduce the identical backtest. Change --set seed=N to sample a
different random sequence (run several seeds; judge the DISTRIBUTION, a
single seed's result is one draw, not a truth).

UNTESTED as a money-maker by design - random entries carry no alpha; net
expectancy is trend-beta minus costs and spread. Use it as the honesty
benchmark for other engines, or for exit-geometry experiments.

All parameters via --set (plus --rr and --max-spread which map directly):

    python run/backtest.py --engine retf --start 2024-01-01
    python run/backtest.py --engine retf --set sl_points=8 --rr 2 --set seed=7
    python run/backtest.py --engine retf --set use_breakeven=true --set entry_prob=0.25
    python run/live.py --engine retf --broker mt5 --mt5-symbol XAUUSD+ --lots 0.01
"""
from __future__ import annotations

import json
import random

from .lrev import BarBuilder, median

RETF_CONFIG = {
    "tf_min": 15,             # trading timeframe in minutes (15/60/240 can
                              # warm up from cached bars; others build live)
    "sl_mode": "points",      # "points" (fixed) | "atr" (mean TR, responsive)
                              # | "mtr" (median TR, outlier-robust)
    "sl_points": 5.0,         # stop distance in points     (sl_mode=points)
    "sl_mult": 1.5,           # stop = this x ATR/MTR       (sl_mode=atr/mtr)
    "vol_window": 20,         # closed bars for the ATR/MTR calculation
    "sl_min_points": 0.5,     # floor for computed stops    (sl_mode=atr/mtr)
    "rr": 3.0,                # TP = stop distance x rr   (flag: --rr)
    "ema_period": 50,         # trend filter length (bars)
    "require_slope": False,   # stricter: EMA must slope with the trade
    "slope_lag": 10,          # bars back for the slope comparison
    "entry_prob": 1.0,        # chance to enter per eligible bar (0..1]
    "use_breakeven": False,   # move SL to entry at +1R
    "use_time_exit": False,   # flatten after max_bars if still open
    "max_bars": 50,
    "session_start": "",      # "HH:MM" UTC; both empty = trade 24h
    "session_end": "",        # supports overnight windows (start > end)
    "reentry_bars": 1,        # bars to wait after a close before re-entry
    "seed": 42,               # RNG seed - same seed + data = same trades
    "max_spread": 0.9,        # entry spread gate ($; per-symbol default)
    "max_concurrent": 1,
    "qty": 1,
    "engine_name": "RETF",
    "tag_prefix": "RF",
}

_TF_NAME = {15: "M15", 60: "H1", 240: "H4"}


def _parse_hhmm(s: str) -> int | None:
    if not s:
        return None
    hh, _, mm = str(s).partition(":")
    return int(hh) * 60 + int(mm or 0)


class RETFStrategy:
    """Bars are built from ticks; the decision runs on the first tick after
    a bar closes and the entry fills AT that tick (no look-ahead).
    Breakeven and the time exit are engine-driven through the generic
    Broker.move_sl_to_breakeven / close_position interfaces (server-side
    on MT5, simulated on paper)."""

    CONFIG = RETF_CONFIG
    CLI_DEFAULTS = dict(RETF_CONFIG)

    def __init__(self, broker, config: dict | None = None, log=print):
        cfg = dict(type(self).CONFIG)
        if config:
            cfg.update(config)    # runner cfg carries L-Rev keys; ignored
        if str(cfg["sl_mode"]).lower() not in ("points", "atr", "mtr"):
            raise SystemExit(f"retf: sl_mode must be points/atr/mtr, "
                             f"got '{cfg['sl_mode']}'")
        self.cfg = cfg
        self.broker = broker
        self.log = log
        self.now = 0
        self.bid = self.ask = float("nan")
        self._bb = BarBuilder(int(cfg["tf_min"]) * 60)
        self._rng = random.Random(int(cfg["seed"]))
        self._alpha = 2.0 / (float(cfg["ema_period"]) + 1.0)
        self._ema = None
        self._ema_n = 0                       # closes consumed by the EMA
        self._ema_hist = []                   # last slope_lag+1 EMA values
        self._bar_count = 0                   # closed bars seen
        self._sess_lo = _parse_hhmm(cfg["session_start"])
        self._sess_hi = _parse_hhmm(cfg["session_end"])
        self._open = None                     # {tag, entry_bar, dir, ref, be}
        self._last_close_bar = None
        self._n_trades = 0

    # ---------------------------------------------------------------- seeding
    def seed_bars(self, tf: str, bars):
        """Warm the EMA and the bar history (for ATR/MTR stops) from cached
        bars of the matching timeframe."""
        if _TF_NAME.get(int(self.cfg["tf_min"])) != tf:
            return
        bars = list(bars)
        self._bb.seed(bars)                 # TR history for atr/mtr stops
        for b in bars:
            self._ema_update(b.c)
        if bars:
            self.log(f"[RETF] warmed from {len(bars)} {tf} bars")

    def _ema_update(self, close: float):
        self._ema = (close if self._ema is None
                     else self._ema + self._alpha * (close - self._ema))
        self._ema_n += 1
        self._ema_hist.append(self._ema)
        lag = int(self.cfg["slope_lag"]) + 1
        if len(self._ema_hist) > lag:
            del self._ema_hist[:-lag]

    # ---------------------------------------------------------------- events
    def on_tick(self, ts: int, price: float, size: float, side: str,
                bid: float, ask: float):
        self.now, self.bid, self.ask = ts, bid, ask
        cfg = self.cfg

        # manage the open position on every tick
        if self._open is not None:
            t = self._open
            if self.broker.open_count(t["tag"]) == 0:      # SL/TP closed it
                self._last_close_bar = self._bar_count
                self._open = None
            else:
                if (cfg["use_breakeven"] and not t["be"]):
                    fav = (price - t["ref"]) * t["dir"]
                    if fav >= t["stop"]:                   # +1R in favor
                        if self.broker.move_sl_to_breakeven(ts, t["tag"]):
                            t["be"] = True

        if self._bb.on_trade(ts, price, size):             # a bar CLOSED
            self._on_bar_close(price)                      # price = the
            # first tick of the NEW bar = the actual market entry price

    # ---------------------------------------------------------------- logic
    def _in_session(self, ts: int) -> bool:
        if self._sess_lo is None or self._sess_hi is None:
            return True
        minute = (ts // 60_000_000_000) % 1440             # UTC time-of-day
        if self._sess_lo <= self._sess_hi:
            return self._sess_lo <= minute < self._sess_hi
        return minute >= self._sess_lo or minute < self._sess_hi

    def _on_bar_close(self, entry_px: float):
        cfg = self.cfg
        self._bar_count += 1
        closed = self._bb.bars[-1]
        self._ema_update(closed.c)

        # time exit for the open position (bar-based, spec S exit logic)
        if self._open is not None and cfg["use_time_exit"]:
            held = self._bar_count - self._open["entry_bar"]
            if held >= int(cfg["max_bars"]):
                self.log(f"[RETF] TIME EXIT after {held} bars "
                         f"[{self._open['tag']}]")
                if self.broker.close_position(self.now, self._open["tag"]):
                    self._last_close_bar = self._bar_count
                    self._open = None

        # ---- entry evaluation ----
        if self._open is not None:
            return                                          # one at a time
        if self.broker.open_count(cfg["tag_prefix"] + "|") \
                >= cfg["max_concurrent"]:
            return
        if (self._last_close_bar is not None
                and self._bar_count - self._last_close_bar
                < int(cfg["reentry_bars"])):
            return                                          # re-entry wait
        if not self._in_session(self.now):
            return
        if self._ema_n < int(cfg["ema_period"]):
            return                                          # filter not warm
        direction = 1 if closed.c > self._ema else (-1 if closed.c < self._ema
                                                    else 0)
        if direction == 0:
            return
        if cfg["require_slope"]:
            if len(self._ema_hist) <= int(cfg["slope_lag"]):
                return
            slope = self._ema_hist[-1] - self._ema_hist[0]
            if slope * direction <= 0:
                return
        if self._rng.random() >= float(cfg["entry_prob"]):
            return                                          # random skip
        spread = self.ask - self.bid
        if cfg["max_spread"] > 0 and spread == spread \
                and spread > cfg["max_spread"]:
            self.log(f"[RETF] entry SKIPPED: spread {spread:.2f} > "
                     f"{cfg['max_spread']}")
            return

        stop = self._stop_distance()
        if stop is None:
            return                          # ATR/MTR not warm - fail closed
        # anchor SL/TP on the ENTRY-time price (this tick), not the closed
        # bar's close - if price gapped over the bar boundary the geometry
        # must follow the fill (spec: SL = entry +- stop)
        ref = entry_px
        sl = ref - direction * stop
        tp = ref + direction * stop * cfg["rr"]
        self._n_trades += 1
        tag = (f"{cfg['tag_prefix']}|{'L' if direction > 0 else 'S'}|"
               f"{self._bar_count}")
        self.log(f"[RETF] {'BUY' if direction > 0 else 'SELL'} "
                 f"(close {ref:.2f} {'>' if direction > 0 else '<'} "
                 f"EMA{cfg['ema_period']} {self._ema:.2f}) "
                 f"SL {sl:.2f} TP {tp:.2f}")
        self.broker.market_order(self.now, direction, cfg["qty"],
                                 sl, tp, tag, ref_px=ref)
        self._open = dict(tag=tag, entry_bar=self._bar_count,
                          dir=direction, ref=ref, stop=stop, be=False)

    def _stop_distance(self) -> float | None:
        """Stop distance per sl_mode: fixed points, or sl_mult x ATR/MTR of
        the last vol_window closed bars (mean = responsive to regime shifts,
        median = robust to single outlier bars). None while not warm."""
        cfg = self.cfg
        mode = str(cfg["sl_mode"]).lower()
        if mode == "points":
            return float(cfg["sl_points"])
        n = int(cfg["vol_window"])
        bars = list(self._bb.bars)
        if len(bars) < n + 1:
            return None
        window = bars[-(n + 1):]            # n TRs need n+1 bars
        trs = [max(c.h - c.l, abs(c.h - p.c), abs(c.l - p.c))
               for p, c in zip(window[:-1], window[1:])]
        base = (sum(trs) / len(trs)) if mode == "atr" else median(trs)
        return max(base * float(cfg["sl_mult"]), float(cfg["sl_min_points"]))

    # ---------------------------------------------------------------- misc
    def status(self) -> str:
        cfg = self.cfg
        ema = f"{self._ema:.2f}" if self._ema is not None else "warming"
        pos = self._open["tag"] if self._open else "flat"
        return (f"EMA{cfg['ema_period']} {ema} | bar {self._bar_count} | "
                f"{self._n_trades} trades | {pos}")

    @staticmethod
    def describe(cfg) -> str:
        opts = []
        if cfg["require_slope"]:
            opts.append("slope-strict")
        if cfg["use_breakeven"]:
            opts.append("breakeven@1R")
        if cfg["use_time_exit"]:
            opts.append(f"time-exit {cfg['max_bars']} bars")
        if cfg["session_start"] and cfg["session_end"]:
            opts.append(f"session {cfg['session_start']}-{cfg['session_end']} UTC")
        extra = (" | " + ", ".join(opts)) if opts else ""
        mode = str(cfg["sl_mode"]).lower()
        sl_txt = (f"SL {cfg['sl_points']} pts" if mode == "points"
                  else f"SL {cfg['sl_mult']}x{mode.upper()}"
                       f"({cfg['vol_window']})")
        return (f"{cfg.get('engine_name', 'RETF')} | RANDOM entries "
                f"(p={cfg['entry_prob']}, seed {cfg['seed']}) with "
                f"EMA{cfg['ema_period']} trend filter on M{cfg['tf_min']} | "
                f"{sl_txt}, TP {cfg['rr']}R{extra}")

    def save_state(self, path):
        with open(path, "w") as f:
            json.dump(dict(engine=self.cfg["engine_name"], ema=self._ema,
                           bars=self._bar_count, trades=self._n_trades,
                           open=self._open), f, indent=1, default=str)
