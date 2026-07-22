"""L-Rev v2 strategy engine - THE single source of truth.

Both the backtest (tick replay over historical TBBO) and live trading
(real-time TBBO stream) execute this exact class; there is no separate
backtest implementation. Consumes TBBO ticks (trade + best bid/offer) and
emits orders to a Broker. Rules:

  Levels    : fractal swing highs/lows, 8 bars each side, on M15/H1/H4,
              confirmed 9 bars after the swing bar.
  Entry     : virtual stop AT the level (buy at swing high + spread-at-detection,
              sell at swing low). One trade per level, ever.
  Exits     : SL = median true range(8 bars, at detection) x mult
              (M15 1.5 / H1 0.5 / H4 0.5), TP = SL x 2.
  Gate 1    : level pruned if untriggered 35h after it becomes armed.
  Gate 2    : trigger ignored (level consumed) if spread > $0.90 at the trigger tick.
  Gate 3    : TBBO flow gate (ON by default) - trigger taken only if the
              30s direction-aligned aggressor imbalance is in [0.0, 0.6]:
              real but not climactic flow. Computed live from the same TBBO
              stream. Disable explicitly with use_flow_gate=False
              (--no-flow-gate in live.py, --config v2-ea in backtest.py).
  Also      : level pruned when price closes beyond it by $0.01 without a fill,
              or 336h after the swing bar formed.

The engine is venue-agnostic: give it any object implementing broker.Broker.
"""
from __future__ import annotations

import collections
import json
import os
from dataclasses import dataclass, field

NS_MIN = 60_000_000_000
TF_SECONDS = {"M15": 900, "H1": 3600, "H4": 14400}

DEFAULT_CONFIG = {
    "timeframes": {"M15": 1.5, "H1": 0.5, "H4": 0.5},  # tf -> SL multiplier
    "rr": 2.0,
    "fractal_bars": 8,
    "max_level_distance": 200.0,
    "level_max_age_h": 336,
    "order_max_age_h": 35,     # gate 1
    "max_spread": 0.90,        # gate 2
    "use_flow_gate": True,     # gate 3 (needs TBBO side data; harmless off)
    "flow_lo": 0.0,
    "flow_hi": 0.6,
    "flow_window_s": 30,
    "qty": 1,
}


@dataclass
class Bar:
    t: int  # open time ns
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class Level:
    tf: str
    price: float
    is_low: bool          # True = swing low -> sell side
    formed_ns: int        # swing bar open time
    detected_ns: int      # when confirmed
    trigger_px: float     # sell: price; buy: price + spread at detection
    sl_dist: float        # MTR x mult at detection
    armed_ns: int = 0     # first moment it was live (age-cap reference)


class BarBuilder:
    """Aggregates trade ticks into fixed-interval bars; keeps last `keep` closed bars."""

    def __init__(self, tf_seconds: int, keep: int = 400):
        self.tf_ns = tf_seconds * 1_000_000_000
        self.bars: collections.deque[Bar] = collections.deque(maxlen=keep)
        self.cur: Bar | None = None

    def seed(self, bars):
        """Preload closed bars (replay warmup or historical bootstrap)."""
        for b in bars:
            self.bars.append(b)

    def on_trade(self, ts: int, px: float, sz: float):
        """Returns True if a bar just CLOSED (i.e. a new bar opened)."""
        t0 = ts - ts % self.tf_ns
        if self.cur is None:
            self.cur = Bar(t0, px, px, px, px, sz)
            return False
        if t0 == self.cur.t:
            c = self.cur
            c.h = max(c.h, px)
            c.l = min(c.l, px)
            c.c = px
            c.v += sz
            return False
        self.bars.append(self.cur)
        self.cur = Bar(t0, px, px, px, px, sz)
        return True


class FlowWindow:
    """Rolling aggressor-imbalance over the last N seconds of trades."""

    def __init__(self, window_s: int):
        self.win_ns = window_s * 1_000_000_000
        self.q: collections.deque = collections.deque()
        self.vol = 0.0
        self.signed = 0.0

    def on_trade(self, ts: int, sz: float, sign: int):
        self.q.append((ts, sz, sign))
        self.vol += sz
        self.signed += sz * sign
        lo = ts - self.win_ns
        while self.q and self.q[0][0] < lo:
            ots, osz, osgn = self.q.popleft()
            self.vol -= osz
            self.signed -= osz * osgn

    def imbalance(self) -> float:
        return self.signed / self.vol if self.vol > 0 else 0.0


def median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


class LRevStrategy:
    """Feed ticks with on_tick(); orders go to the broker; state is queryable."""

    def __init__(self, broker, config: dict | None = None, log=print):
        self.cfg = dict(DEFAULT_CONFIG, **(config or {}))
        self.broker = broker
        self.log = log
        self.builders = {tf: BarBuilder(TF_SECONDS[tf]) for tf in self.cfg["timeframes"]}
        self.levels: list[Level] = []
        self.flow = FlowWindow(self.cfg["flow_window_s"])
        self.bid = float("nan")
        self.ask = float("nan")
        self.now = 0
        self._max_low = float("-inf")   # fast-path trigger bounds
        self._min_high = float("inf")

    # ---------------------------------------------------------------- seeding
    def seed_bars(self, tf: str, bars):
        self.builders[tf].seed(bars)

    # ---------------------------------------------------------------- events
    def on_tick(self, ts: int, price: float, size: float, side: str,
                bid: float, ask: float):
        """side: 'B' buy-aggressor, 'A' sell-aggressor, 'N' unknown."""
        self.now = ts
        self.bid, self.ask = bid, ask
        sign = 1 if side == "B" else (-1 if side == "A" else 0)
        self.flow.on_trade(ts, size, sign)

        bar_closed = False
        for tf, bb in self.builders.items():
            if bb.on_trade(ts, price, size):
                self._on_bar_close(tf)
                bar_closed = True

        if bid <= self._max_low or ask >= self._min_high:
            self._check_triggers()
        if bar_closed:
            self._prune()

    # ---------------------------------------------------------------- swings
    def _on_bar_close(self, tf: str):
        fb = self.cfg["fractal_bars"]
        bars = self.builders[tf].bars
        need = 2 * fb + 1
        if len(bars) < need:
            return
        w = list(bars)[-need:]          # candidate is w[fb]
        cand = w[fb]
        others_h = [b.h for i, b in enumerate(w) if i != fb]
        others_l = [b.l for i, b in enumerate(w) if i != fb]
        spread = (self.ask - self.bid) if self.ask == self.ask else 0.0

        if not any(h > cand.h for h in others_h):
            self._add_level(tf, cand.h, False, cand.t, spread)
        if not any(l < cand.l for l in others_l):
            self._add_level(tf, cand.l, True, cand.t, spread)

    def _mtr(self, tf: str) -> float:
        """Median true range of the last `fractal_bars` CLOSED bars,
        excluding the most recent closed bar (EA parity)."""
        fb = self.cfg["fractal_bars"]
        bars = list(self.builders[tf].bars)
        if len(bars) < fb + 2:
            return 0.0
        window = bars[-(fb + 2):-1]      # fb+1 bars: fb for TR + 1 older for prev close
        trs = []
        for prev, cur in zip(window[:-1], window[1:]):
            trs.append(max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c)))
        return median(trs)

    def _level_known(self, tf, price, is_low):
        # EA parity (L_LevelKnown): dedup against CURRENTLY ACTIVE levels only
        return any(lv.tf == tf and lv.is_low == is_low and
                   abs(lv.price - price) < 0.01 for lv in self.levels)

    def _rebounds(self):
        self._max_low = max((lv.trigger_px for lv in self.levels if lv.is_low),
                            default=float("-inf"))
        self._min_high = min((lv.trigger_px for lv in self.levels if not lv.is_low),
                             default=float("inf"))

    def _add_level(self, tf, price, is_low, formed_ns, spread):
        if self._level_known(tf, price, is_low):
            return
        # already broken?
        if is_low and self.bid < price - 0.01:
            return
        if not is_low and self.bid > price + 0.01:
            return
        dist = (self.bid - price) if is_low else (price - self.bid)
        if dist <= 0 or dist > self.cfg["max_level_distance"]:
            return
        m = self._mtr(tf)
        if m <= 0:
            return
        lv = Level(tf=tf, price=price, is_low=is_low, formed_ns=formed_ns,
                   detected_ns=self.now,
                   trigger_px=price if is_low else price + spread,
                   sl_dist=m * self.cfg["timeframes"][tf],
                   armed_ns=self.now)
        self.levels.append(lv)
        self._rebounds()
        self.log(f"[{tf}] level {'LOW' if is_low else 'HIGH'} {price:.2f} "
                 f"(SLdist {lv.sl_dist:.2f})")

    # ---------------------------------------------------------------- triggers
    def _check_triggers(self):
        if not (self.bid == self.bid and self.ask == self.ask):
            return
        spread = self.ask - self.bid
        for lv in list(self.levels):
            hit = (self.bid <= lv.trigger_px) if lv.is_low \
                else (self.ask >= lv.trigger_px)
            if not hit:
                continue
            self.levels.remove(lv)      # one shot per level, taken or not
            self._rebounds()
            if self.cfg["max_spread"] > 0 and spread > self.cfg["max_spread"]:
                self.log(f"[{lv.tf}] trigger {lv.price:.2f} SKIPPED: spread {spread:.2f}")
                continue
            direction = -1 if lv.is_low else 1
            if self.cfg["use_flow_gate"]:
                aligned = self.flow.imbalance() * direction
                if not (self.cfg["flow_lo"] <= aligned <= self.cfg["flow_hi"]):
                    self.log(f"[{lv.tf}] trigger {lv.price:.2f} SKIPPED: "
                             f"flow {aligned:+.3f} outside "
                             f"[{self.cfg['flow_lo']},{self.cfg['flow_hi']}]")
                    continue
            sl_d = lv.sl_dist
            tp_d = sl_d * self.cfg["rr"]
            px = lv.trigger_px
            sl = px + sl_d if lv.is_low else px - sl_d
            tp = px - tp_d if lv.is_low else px + tp_d
            self.broker.market_order(
                ts=self.now, direction=direction, qty=self.cfg["qty"],
                sl=sl, tp=tp, ref_px=px,
                tag=f"L-Rev|{lv.tf}|{'low' if lv.is_low else 'high'}@{lv.price:.2f}")

    # ---------------------------------------------------------------- pruning
    def _prune(self):
        if not self.levels:
            return
        max_age = self.cfg["level_max_age_h"] * 3600 * 1_000_000_000
        max_wait = self.cfg["order_max_age_h"] * 3600 * 1_000_000_000
        keep = []
        for lv in self.levels:
            dead = (
                (lv.is_low and self.bid < lv.price - 0.01) or
                (not lv.is_low and self.bid > lv.price + 0.01) or
                (self.now - lv.formed_ns > max_age) or
                (self.cfg["order_max_age_h"] > 0 and
                 self.now - lv.armed_ns > max_wait)
            )
            if not dead:
                keep.append(lv)
        if len(keep) != len(self.levels):
            self.levels = keep
            self._rebounds()

    # ---------------------------------------------------------------- state
    def snapshot(self) -> dict:
        return {
            "now": self.now,
            "levels": [vars(lv) for lv in self.levels],
            "bid": self.bid, "ask": self.ask,
        }

    def save_state(self, path):
        with open(path, "w") as f:
            json.dump(self.snapshot(), f, indent=2)
