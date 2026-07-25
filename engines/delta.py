"""Delta-1m: 1-minute volume-delta breakout engine.

RULE (user-specified): at each completed 1-minute candle, compute the
aggressor volume delta from TBBO sides:

    delta% = (buy volume - sell volume) / (buy volume + sell volume)

where "buy" = trades that lifted the ask, "sell" = trades that hit the bid
(unclassified ticks are excluded). If delta% >= +threshold -> BUY on the
next tick; delta% <= -threshold -> SELL. Stop-loss = that candle's LOW for
a buy / HIGH for a sell; take-profit = RR x the stop distance. Delta sign
decides direction; set require_color=true to also demand the candle closed
in the same direction.

Gates: |delta%| >= delta_threshold, classified volume >= min_volume,
stop distance >= min_sl_dist, quoted spread <= max_spread, and fewer than
max_concurrent open positions.

STATUS: UNTESTED - built for the user's own backtesting; no research or
validation has been performed on this engine. Fair warning from the
G-Trend research (docs/GTREND_SPEC.md): 1-minute strategies on GC
(order-flow, breakout, mean-reversion) historically LOST to trading costs
(~0.4 pt/RT + spread vs typical 1-min ranges of 0.5-2 pts). Validate on
2023-2024, confirm on 2025-2026, and mind the trade count x cost drag.

All parameters are configurable from the command line via the generic
--set flag (plus --rr and --max-spread, which map directly):

    python run/backtest.py --engine delta --set delta_threshold=0.7 --rr 2.0
    python run/backtest.py --engine delta --set min_volume=100 --set require_color=true
    python run/live.py --engine delta --broker mt5 --mt5-symbol XAUUSD+ --lots 0.01
"""
from __future__ import annotations

import json

DELTA_CONFIG = {
    "delta_threshold": 0.6,   # |delta%| needed, 0..1 (0.6 = 60% one-sided)
    "rr": 1.5,                # TP = RR x SL distance
    "min_volume": 50,         # classified contracts required in the candle
    "min_sl_dist": 0.2,       # points; skip if the candle gives a ~zero stop
    "require_color": False,   # candle close must agree with delta direction
    "max_concurrent": 1,      # simultaneous open positions
    "max_spread": 0.9,        # skip entries when quoted spread > this ($)
    "qty": 1,
    "engine_name": "Delta-1m",
    "tag_prefix": "DLT",
}

_MIN_NS = 60 * 1_000_000_000


class DeltaStrategy:
    """1-min volume-delta engine. Candles are built from ticks; the signal
    is evaluated on the first tick after a candle completes and the entry
    fires AT that tick (no look-ahead: the candle is fully closed, the
    fill is the next traded price). Exits are the broker's SL/TP
    (server-side on MT5, tick-simulated on paper)."""

    WARMUP_DAYS = 1              # needs no history - one candle is enough
    CONFIG = DELTA_CONFIG
    CLI_DEFAULTS = dict(DELTA_CONFIG)

    def __init__(self, broker, config: dict | None = None, log=print):
        cfg = dict(type(self).CONFIG)
        if config:
            cfg.update(config)   # runner cfg also carries L-Rev keys; ignored
        self.cfg = cfg
        self.broker = broker
        self.log = log
        self.now = 0
        self.bid = self.ask = float("nan")
        # current 1-min candle
        self._t0 = None          # candle open time (ns, minute-aligned)
        self._o = self._h = self._l = self._c = None
        self._buy = self._sell = 0.0
        # last completed candle's stats (for status/state)
        self._last = None
        self._n_signals = 0

    # ---------------------------------------------------------------- seeding
    def seed_bars(self, tf: str, bars):
        """No warmup needed - the signal only uses the current candle."""

    # ---------------------------------------------------------------- events
    def on_tick(self, ts: int, price: float, size: float, side: str,
                bid: float, ask: float):
        self.now, self.bid, self.ask = ts, bid, ask
        t0 = ts - ts % _MIN_NS
        if self._t0 is None:
            self._start(t0, price)
        elif t0 != self._t0:
            self._close_candle()             # evaluate the finished candle
            self._start(t0, price)           # this tick opens the new one
            # entry (if armed) fires at THIS tick = first price after close
        self._h = max(self._h, price)
        self._l = min(self._l, price)
        self._c = price
        if side == "B":
            self._buy += size
        elif side == "A":
            self._sell += size

    def _start(self, t0, price):
        self._t0 = t0
        self._o = self._h = self._l = self._c = price
        self._buy = self._sell = 0.0

    # ---------------------------------------------------------------- signal
    def _close_candle(self):
        cfg = self.cfg
        o, h, l, c = self._o, self._h, self._l, self._c
        buy, sell = self._buy, self._sell
        vol = buy + sell
        if vol <= 0:
            return
        delta = (buy - sell) / vol
        self._last = dict(o=o, h=h, l=l, c=c, vol=vol, delta=delta)
        if abs(delta) < cfg["delta_threshold"]:
            return
        if vol < cfg["min_volume"]:
            return
        direction = 1 if delta > 0 else -1
        if cfg["require_color"]:
            if direction > 0 and c <= o:
                return
            if direction < 0 and c >= o:
                return
        spread = self.ask - self.bid
        if cfg["max_spread"] > 0 and spread == spread \
                and spread > cfg["max_spread"]:
            self.log(f"[Delta-1m] SKIPPED delta {delta:+.2f}: "
                     f"spread {spread:.2f} > {cfg['max_spread']}")
            return
        if self.broker.open_count(cfg["tag_prefix"] + "|") \
                >= cfg["max_concurrent"]:
            return
        ref = c                              # ~ the next tick's price
        sl = l if direction > 0 else h       # candle low / high
        dist = (ref - sl) if direction > 0 else (sl - ref)
        if dist < cfg["min_sl_dist"]:
            return                           # candle closed on its extreme
        tp = ref + direction * cfg["rr"] * dist
        tag = (f"{cfg['tag_prefix']}|{'L' if direction > 0 else 'S'}|"
               f"{self._t0 // _MIN_NS % 100000}")
        self._n_signals += 1
        self.log(f"[Delta-1m] {'BUY' if direction > 0 else 'SELL'} "
                 f"delta {delta:+.2f} vol {vol:.0f} | SL {sl:.2f} "
                 f"TP {tp:.2f} (risk {dist:.2f})")
        self.broker.market_order(self.now, direction, cfg["qty"],
                                 sl, tp, tag, ref_px=ref)

    # ---------------------------------------------------------------- misc
    def status(self) -> str:
        cfg = self.cfg
        n_open = self.broker.open_count(cfg["tag_prefix"] + "|")
        if self._last is None:
            return "Delta-1m: waiting for first candle"
        return (f"last delta {self._last['delta']:+.2f} "
                f"(thr {cfg['delta_threshold']}) | vol {self._last['vol']:.0f} | "
                f"{self._n_signals} signals | "
                f"{n_open}/{cfg['max_concurrent']} open")

    @staticmethod
    def describe(cfg) -> str:
        col = " | candle color must agree" if cfg["require_color"] else ""
        return (f"{cfg.get('engine_name', 'Delta-1m')} | 1-min volume delta "
                f">= {cfg['delta_threshold']:.0%} | SL = candle extreme, "
                f"TP = {cfg['rr']} x risk | vol >= {cfg['min_volume']} | "
                f"spread <= {cfg['max_spread']} | "
                f"max {cfg['max_concurrent']} open{col}")

    def save_state(self, path):
        with open(path, "w") as f:
            json.dump(dict(engine=self.cfg["engine_name"],
                           signals=self._n_signals, last=self._last), f,
                      indent=1, default=str)
