"""L-Def: level-DEFEND engine (separate from the validated L-Rev break engine).

EXPERIMENTAL - TESTED AND CURRENTLY FAILED. Do not trade this.

Test record (Dec 2025 - May 2026 in-sample, GC tick data, same costs as L-Rev):
    default (RR 2):              33 trades, -355 pts, PF 0.27, win 15%
    RR 1.0:                      33 trades, -298 pts, PF 0.30
    RR 1.5:                      33 trades, -312 pts, PF 0.32
    wide stops (3.0/1.0/1.0):    33 trades, -415 pts, PF 0.46
    strict stop-run (flow<-0.2):  4 trades,  -39 pts, PF 0.23
Every variant deeply negative. Weak-flow breaks do revert on average (the
drift research showed that), but the bounce is too small and too slow to pay
for spread + stop geometry - the same reason the CB-fade failed. Kept in the
repo so the result is reproducible and so future regime data (e.g. low-vol
2023-2024) can re-test it:  python backtest.py --engine ldef --start 2023-01-01

Idea (from the TBBO research): when a swing level breaks WITHOUT confirming
aggressor flow, it is usually a stop-run - resting stops get triggered, no
real participation, and price tends to snap back into the range. L-Rev's
flow gate SKIPS those breaks; L-Def TRADES them, in the opposite direction:

    swing LOW swept  with weak/opposing flow  ->  BUY the bounce
    swing HIGH swept with weak/opposing flow  ->  SELL the rejection

Shares ALL level machinery and settings with the L-Rev engine (fractal
levels, MTR-based SL, RR-based TP, spread gate, age caps). Only the trigger
decision differs:
  - flow condition: aligned-with-BREAK flow must be inside
    [flow_lo, flow_hi], defaulting to [-1.0, 0.0] (break NOT confirmed)
  - trade direction: OPPOSITE of the break
  - trade tag prefix: "L-Def"

Both engines can run in parallel later (different tags/magic separation),
but only after L-Def independently proves itself in backtests.
"""
from __future__ import annotations

from .strategy import DEFAULT_CONFIG, LRevStrategy

# Same settings as the break engine, except the flow band semantics:
# in L-Def, [flow_lo, flow_hi] bounds the BREAK-aligned flow that marks a
# suspected stop-run worth fading.
DEFEND_CONFIG = dict(DEFAULT_CONFIG)
DEFEND_CONFIG.update({
    "flow_lo": -1.0,
    "flow_hi": 0.0,
    "engine_name": "L-Def",
})


class LDefStrategy(LRevStrategy):
    """Level-defend engine. Reuses LRevStrategy's level detection, pruning,
    gates and plumbing; overrides only the trigger action."""

    def __init__(self, broker, config: dict | None = None, log=print):
        cfg = dict(DEFEND_CONFIG)
        if config:
            for k, v in config.items():
                if k == "timeframes":
                    cfg["timeframes"] = dict(v)
                else:
                    cfg[k] = v
        super().__init__(broker, config=cfg, log=log)

    # ---------------------------------------------------------------- trigger
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
                self.log(f"[{lv.tf}] trigger {lv.price:.2f} SKIPPED: "
                         f"spread {spread:.2f}")
                continue
            d_break = -1 if lv.is_low else 1        # direction of the BREAK
            if self.cfg["use_flow_gate"]:
                aligned = self.flow.imbalance() * d_break
                if not (self.cfg["flow_lo"] <= aligned <= self.cfg["flow_hi"]):
                    self.log(f"[{lv.tf}] trigger {lv.price:.2f} SKIPPED: "
                             f"break flow {aligned:+.3f} outside "
                             f"[{self.cfg['flow_lo']},{self.cfg['flow_hi']}] "
                             f"(not a stop-run)")
                    continue
            direction = -d_break                     # DEFEND: fade the break
            sl_d = lv.sl_dist
            tp_d = sl_d * self.cfg["rr"]
            px = lv.trigger_px
            if direction > 0:            # buy the swept low: SL below, TP above
                sl = px - sl_d
                tp = px + tp_d
            else:                        # sell the swept high: SL above, TP below
                sl = px + sl_d
                tp = px - tp_d
            self.broker.market_order(
                ts=self.now, direction=direction, qty=self.cfg["qty"],
                sl=sl, tp=tp, ref_px=px,
                tag=f"L-Def|{lv.tf}|{'low' if lv.is_low else 'high'}@{lv.price:.2f}")
