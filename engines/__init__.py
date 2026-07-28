"""Strategy engines. ONE file per engine; add new engines here.

Adding an engine = one file in engines/ + one line in ENGINES below.
The whole infrastructure then applies to it automatically, with NO changes
to any other file:

  - run/backtest.py --engine <name>   (any dates, any symbol, --symbols
    GC,SI portfolio mode, cost model, full terminal report + monthly tables)
  - run/live.py --engine <name>       (paper or MT5 execution, multi-symbol
    supervisor, auto-reconnect, session logs, state snapshots)
  - all shared CLI flags (--rr, --sl-*, --tf, --max-spread, --order-age,
    --flow-lo/hi) and per-symbol defaults from core/symbols.py

Contract: the class takes (broker, config=dict, log=fn) and implements
on_tick(ts, price, size, side, bid, ask), seed_bars(tf, bars) and
save_state(path), sending orders through core.broker.Broker.market_order.
Simplest path: subclass LRevStrategy and override what differs (see
ldef.py). Optional class attribute CLI_DEFAULTS = {...} declares config
overrides the runners apply as the engine's base (explicit CLI flags still
win) - e.g. ldef sets its own flow band and engine_name there.
"""
from .delta import DeltaStrategy
from .gtrend import GTrendLowDD, GTrendStrategy
from .ldef import DEFEND_CONFIG, LDefStrategy
from .lrev import DEFAULT_CONFIG, Bar, LRevStrategy, TF_SECONDS
from .sweepfade import SweepFadeStrategy

ENGINES = {
    "lrev": LRevStrategy,       # level-BREAK engine (validated on GC, OOS on SI)
    "ldef": LDefStrategy,       # level-DEFEND engine (experimental; tested negative)
    "gtrend": GTrendStrategy,   # daily trend-pullback PRIMARY (spec: docs/GTREND_SPEC.md)
    "gtrend-lowdd": GTrendLowDD,  # same rules, LOW-DD sizing (3 x 1/3, z>=0.6)
    "delta": DeltaStrategy,     # 1-min volume-delta breakout (UNTESTED - backtest first)
    "sweepfade": SweepFadeStrategy,  # fade BIG sweeps (spec: docs/SWEEPFADE_SPEC.md; small sample)
}
