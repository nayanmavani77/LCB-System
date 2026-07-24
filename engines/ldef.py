"""L-Def v2: level-DEFEND engine (separate from the validated L-Rev break engine).

EXPERIMENTAL - TESTED, NOT PROFITABLE ON TRENDING-REGIME DATA. Do not trade.

v2 test record (Dec 2025 - May 2026 in-sample, GC ticks, same costs as L-Rev):
    default (flow>=0.2, RR 2):   307 trades, -336 pts, PF 0.90, win 35%
    RR 1.0:                      307 trades, -228 pts, PF 0.91, win 53%
    stronger defense (>=0.3):    231 trades, -384 pts, PF 0.85
    >=0.3 + RR 1.5:              231 trades, -404 pts, PF 0.83
Far better than v1 (PF 0.27) but consistently negative, and CRUCIALLY:
demanding STRONGER defense flow made results WORSE - the opposite of the
hypothesis's prediction. Untested on low-volatility 2023-2024 data, where
range-bound conditions favor defense:
    python backtest.py --engine ldef --start 2023-01-01 --end 2025-01-01

HYPOTHESIS (v2): when price comes down to TEST a swing level and aggressive
flow appears IN THE DEFENSE DIRECTION at the touch (buyers lifting the ask
at a swing low / sellers hitting the bid at a swing high - defenders
actively absorbing the test), the level holds and price bounces. Trade the
bounce, but only after confirmation:

    1. TOUCH    : price reaches the level (bid <= swing low / ask >= swing high)
    2. WATCH    : a confirmation window opens (confirm_window_s, default 90s)
    3. FAIL if  : price penetrates beyond the level by more than
                  max_break_frac x SL-distance (real break, not a test), or
                  the window expires without confirmation
    4. CONFIRM  : price is back at/above (below) the level AND the 30s
                  aggressor imbalance aligned with the DEFENSE is inside
                  [flow_lo, flow_hi] (default [0.2, 1.0] - active defense)
    5. ENTER    : market order in the defense direction; SL = MTR x tf-mult
                  below entry (above for shorts), TP = SL x RR.

Shares all level machinery and settings with L-Rev (fractal levels, MTR
geometry, spread gate, age caps). Run with:
    python backtest.py --engine ldef --start ... --end ...

Historical note - v1 ("fade the weak-flow break": enter opposite the break
instantly when break-aligned flow <= 0) FAILED decisively in-sample
(PF 0.27-0.46 across all variants, Dec 2025-May 2026). v2 is the stricter
active-defense formulation of the idea.
"""
from __future__ import annotations

from dataclasses import dataclass

from .lrev import DEFAULT_CONFIG, Level, LRevStrategy

DEFEND_CONFIG = dict(DEFAULT_CONFIG)
DEFEND_CONFIG.update({
    # bounce-aligned aggressor flow required to confirm active defense
    "flow_lo": 0.2,
    "flow_hi": 1.0,
    "confirm_window_s": 90,   # seconds after touch to wait for confirmation
    "max_break_frac": 0.5,    # test may penetrate at most this x SL-distance
    "engine_name": "L-Def",
})


@dataclass
class Test:
    lv: Level
    touch_ns: int
    extreme: float   # deepest penetration seen so far


class LDefStrategy(LRevStrategy):
    """Level-defend engine: touch -> active-defense confirmation -> bounce."""

    def __init__(self, broker, config: dict | None = None, log=print):
        cfg = dict(DEFEND_CONFIG)
        if config:
            for k, v in config.items():
                if k == "timeframes":
                    cfg["timeframes"] = dict(v)
                else:
                    cfg[k] = v
        super().__init__(broker, config=cfg, log=log)
        self.testing: list[Test] = []

    # ------------------------------------------------------------- tick hook
    def on_tick(self, ts, price, size, side, bid, ask):
        super().on_tick(ts, price, size, side, bid, ask)
        if self.testing:
            self._update_testing()

    # ------------------------------------------------------------- touch
    def _check_triggers(self):
        """A 'trigger' here is only the TOUCH - it opens a test, no trade yet."""
        if not (self.bid == self.bid and self.ask == self.ask):
            return
        for lv in list(self.levels):
            hit = (self.bid <= lv.trigger_px) if lv.is_low \
                else (self.ask >= lv.trigger_px)
            if not hit:
                continue
            self.levels.remove(lv)      # one test per level, ever
            self._rebounds()
            self.testing.append(Test(
                lv=lv, touch_ns=self.now,
                extreme=self.bid if lv.is_low else self.ask))
            self.log(f"[{lv.tf}] level {lv.price:.2f} TOUCHED - watching for "
                     f"defense ({self.cfg['confirm_window_s']}s window)")

    # ------------------------------------------------------------- defense
    def _update_testing(self):
        win_ns = int(self.cfg["confirm_window_s"] * 1e9)
        spread = self.ask - self.bid
        keep = []
        for t in self.testing:
            lv = t.lv
            # track penetration depth
            if lv.is_low:
                t.extreme = min(t.extreme, self.bid)
                penetration = lv.trigger_px - t.extreme
            else:
                t.extreme = max(t.extreme, self.ask)
                penetration = t.extreme - lv.trigger_px
            # FAIL: real break, not a test
            if penetration > self.cfg["max_break_frac"] * lv.sl_dist:
                self.log(f"[{lv.tf}] test {lv.price:.2f} FAILED: broke "
                         f"{penetration:.2f} beyond (real break)")
                continue
            # FAIL: window expired without confirmation
            if self.now - t.touch_ns > win_ns:
                self.log(f"[{lv.tf}] test {lv.price:.2f} EXPIRED: no defense "
                         f"within window")
                continue
            # CONFIRM: price held back at/above the level + active defense flow
            back = (self.bid >= lv.trigger_px) if lv.is_low \
                else (self.ask <= lv.trigger_px)
            if back:
                d_def = 1 if lv.is_low else -1        # defense = bounce direction
                aligned = self.flow.imbalance() * d_def
                if self.cfg["flow_lo"] <= aligned <= self.cfg["flow_hi"] and \
                        (self.cfg["max_spread"] <= 0 or
                         spread <= self.cfg["max_spread"]):
                    px = self.ask if d_def > 0 else self.bid
                    sl_d = lv.sl_dist
                    tp_d = sl_d * self.cfg["rr"]
                    sl = px - sl_d if d_def > 0 else px + sl_d
                    tp = px + tp_d if d_def > 0 else px - tp_d
                    self.broker.market_order(
                        ts=self.now, direction=d_def, qty=self.cfg["qty"],
                        sl=sl, tp=tp, ref_px=px,
                        tag=f"L-Def|{lv.tf}|"
                            f"{'low' if lv.is_low else 'high'}@{lv.price:.2f}")
                    self.log(f"[{lv.tf}] level {lv.price:.2f} DEFENDED "
                             f"(flow {aligned:+.2f}) -> "
                             f"{'BUY' if d_def > 0 else 'SELL'}")
                    continue                     # test consumed by the trade
            keep.append(t)
        self.testing = keep
