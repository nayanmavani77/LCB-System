"""Backtest runner - executes the EXACT same engine (lrev/) that live trading uses.

The full metrics report (overall + yearly + monthly + timeframe + direction
+ exit-reason tables) prints in the terminal right after every run.

    python3 backtest.py --start 2026-04-01 --end 2026-07-17
    python3 backtest.py --config v2-ea --csv trades.csv
    python3 backtest.py --config v1            # original L-System, gates off

Configs:
    v2-flow  age cap 35h + spread cap $0.90 + TBBO flow gate [0, 0.6]  (default)
    v2-ea    age cap + spread cap only
    v1       no gates (original L-System behaviour)

Dates outside the cached data are clamped. Build the cache first with
scripts/prep.py. P&L is net of quoted spread at fill + 0.15 pts/round turn.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from lrev.cli import add_strategy_args, config_from_args, describe
from lrev.data import cache_for, data_bounds, replay_window
from lrev.symbols import get_symbol

CONFIGS = {
    "v2-flow": {"use_flow_gate": True},
    "v2-ea": {"use_flow_gate": False},
    "v1": {"use_flow_gate": False, "order_max_age_h": 0, "max_spread": 0.0},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbol", default="GC",
                    help="symbol from lrev/symbols.py (default GC); needs its "
                         "cache built via scripts/prep.py --symbol X")
    ap.add_argument("--config", default="v2-flow", choices=sorted(CONFIGS))
    ap.add_argument("--engine", default="lrev", choices=["lrev", "ldef"],
                    help="lrev = level BREAK engine (validated); "
                         "ldef = level DEFEND engine (experimental, lrev/defend.py)")
    ap.add_argument("--csv", default=None, help="save the trade list to CSV")
    ap.add_argument("--cost", type=float, default=None,
                    help="commission+slippage per round turn in price units "
                         "(default: per-symbol value from lrev/symbols.py; "
                         "quoted spread is separately embedded in fill prices)")
    ap.add_argument("--verbose", action="store_true", help="print every signal/fill")
    add_strategy_args(ap)
    args = ap.parse_args()

    sym = get_symbol(args.symbol)
    cache = cache_for(sym["name"])
    if not os.path.exists(os.path.join(cache, "segments.json")):
        raise SystemExit(f"no cache for {sym['name']} at {cache} - put DBN files "
                         f"in Data/{sym['name']}/ and run: "
                         f"python scripts/prep.py --symbol {sym['name']}")
    cost = args.cost if args.cost is not None else sym["cost_pts"]

    d0, d1 = data_bounds(cache)
    t0 = max(pd.Timestamp(args.start, tz="UTC"), d0) if args.start else d0
    t1 = min(pd.Timestamp(args.end, tz="UTC"), d1) if args.end else d1
    if t1 <= t0:
        raise SystemExit(f"empty window; data covers {d0.date()} .. {d1.date()}")
    if (args.start and pd.Timestamp(args.start, tz="UTC") < d0) or \
       (args.end and pd.Timestamp(args.end, tz="UTC") > d1):
        print(f"note: window clamped to available data -> {t0.date()} .. {t1.date()}")

    if args.csv and os.path.exists(args.csv):
        os.remove(args.csv)
    cfg = config_from_args(args, base=CONFIGS[args.config])
    if args.max_spread is None:
        cfg["max_spread"] = sym["max_spread"]   # per-symbol default gate
    strategy_cls = None
    if args.engine == "ldef":
        from lrev.defend import DEFEND_CONFIG, LDefStrategy
        strategy_cls = LDefStrategy
        # defend flow-band defaults unless user overrode them
        if args.flow_lo is None:
            cfg["flow_lo"] = DEFEND_CONFIG["flow_lo"]
        if args.flow_hi is None:
            cfg["flow_hi"] = DEFEND_CONFIG["flow_hi"]
        cfg["engine_name"] = "L-Def"
    print(f"symbol: {sym['name']} (point value ${sym['point_value']:,.0f}/contract, "
          f"cost {cost}/RT)")
    print("strategy:", describe(cfg))
    broker = replay_window(start=t0, end=t1, config=cfg, cache=cache,
                           trade_log_path=args.csv,
                           log=(print if args.verbose else None),
                           strategy_cls=strategy_cls, cost_pts=cost,
                           point_value=sym["point_value"])

    from lrev.report import print_report
    print_report(broker, point_value=sym["point_value"],
                 title=f"BACKTEST RESULT  [{sym['name']} {args.config}]  "
                       f"{t0.date()} .. {t1.date()}")
    if args.csv:
        print("\ntrade log:", args.csv)


if __name__ == "__main__":
    main()
