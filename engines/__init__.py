"""Strategy engines. ONE file per engine; add new engines here.

Each engine is a class with on_tick(ts, price, size, side, bid, ask) that
sends orders to a core.broker.Broker. Register it in ENGINES below and it
becomes available as backtest.py/live.py --engine <name>.
"""
from .ldef import DEFEND_CONFIG, LDefStrategy
from .lrev import DEFAULT_CONFIG, Bar, LRevStrategy, TF_SECONDS

ENGINES = {
    "lrev": LRevStrategy,   # level-BREAK engine (validated on GC, OOS on SI)
    "ldef": LDefStrategy,   # level-DEFEND engine (experimental; tested negative)
}
