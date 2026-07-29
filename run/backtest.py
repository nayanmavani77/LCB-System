"""Backtest runner - replays historical ticks through the EXACT same engine that live trading uses (engines/ + core/).

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
scripts/prep.py. P&L is net of the quoted spread at fill plus a
commission+slippage charge per round turn (--cost; default per-symbol
from core/symbols.py, GC = 0.4 pts).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.cli import add_strategy_args, config_from_args, describe
from core.data import cache_for, data_bounds, replay_window
from core.paths import log_path
from core.symbols import get_symbol

CONFIGS = {
    "v2-flow": {"use_flow_gate": True},
    "v2-ea": {"use_flow_gate": False},
    "v1": {"use_flow_gate": False, "order_max_age_h": 0, "max_spread": 0.0},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", "--symbol", dest="symbols", default="GC",
                    help="one or more symbols, comma-separated (e.g. GC or "
                         "GC,SI). One symbol -> detailed report. Several -> "
                         "detailed report per symbol PLUS a combined "
                         "portfolio section (joint $ P&L and joint max DD, "
                         "as if run in parallel in one account).")
    ap.add_argument("--config", default="v2-flow", choices=sorted(CONFIGS))
    from engines import ENGINES
    ap.add_argument("--engine", default="lrev", choices=sorted(ENGINES),
                    help="strategy engine from the engines/__init__.py "
                         "registry (lrev = validated level-break)")
    ap.add_argument("--csv", default=None, help="save the trade list to CSV")
    ap.add_argument("--cost", type=float, default=None,
                    help="commission+slippage per round turn in price units "
                         "(default: per-symbol value from core/symbols.py; "
                         "quoted spread is separately embedded in fill prices)")
    ap.add_argument("--verbose", action="store_true", help="print every signal/fill")
    add_strategy_args(ap)
    args = ap.parse_args()

    names = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    from core.report import print_portfolio, print_report
    port_dfs = []
    for name in names:
        sym = get_symbol(name)
        cache = cache_for(sym["name"])
        if not os.path.exists(os.path.join(cache, "segments.json")):
            print(f"\n-- {sym['name']}: NO CACHE, skipped. Put DBN files in "
                  f"Data/{sym['name']}/ and run: "
                  f"python scripts/prep.py --symbol {sym['name']}")
            continue
        cost = args.cost if args.cost is not None else sym["cost_pts"]

        d0, d1 = data_bounds(cache)
        t0 = max(pd.Timestamp(args.start, tz="UTC"), d0) if args.start else d0
        t1 = min(pd.Timestamp(args.end, tz="UTC"), d1) if args.end else d1
        if t1 <= t0:
            print(f"\n-- {sym['name']}: empty window "
                  f"(data covers {d0.date()} .. {d1.date()}), skipped")
            continue
        if (args.start and pd.Timestamp(args.start, tz="UTC") < d0) or \
           (args.end and pd.Timestamp(args.end, tz="UTC") > d1):
            print(f"note: {sym['name']} window clamped to available data -> "
                  f"{t0.date()} .. {t1.date()}")

        csv_path = log_path(args.csv) if args.csv else None
        if csv_path and len(names) > 1:
            root, ext = os.path.splitext(csv_path)
            csv_path = f"{root}_{sym['name']}{ext or '.csv'}"
        if csv_path and os.path.exists(csv_path):
            try:
                os.remove(csv_path)
            except OSError:
                # e.g. the file is open in Excel on Windows - do not crash,
                # and do not append into stale rows either: pick a new name
                root, ext = os.path.splitext(csv_path)
                csv_path = f"{root}_{pd.Timestamp.now():%Y%m%d%H%M%S}{ext}"
                print(f"note: old CSV is locked; writing to {csv_path}")

        strategy_cls = ENGINES[args.engine]
        base = dict(CONFIGS[args.config])
        base.update(getattr(strategy_cls, "CLI_DEFAULTS", {}))
        cfg = config_from_args(args, base=base)
        if args.max_spread is None:
            cfg["max_spread"] = sym["max_spread"]   # per-symbol default gate
        print(f"\nsymbol: {sym['name']} (point value "
              f"${sym['point_value']:,.0f}/contract, cost {cost}/RT)")
        print("strategy:", strategy_cls.describe(cfg)
              if hasattr(strategy_cls, "describe") else describe(cfg))
        broker = replay_window(start=t0, end=t1, config=cfg, cache=cache,
                               trade_log_path=csv_path,
                               log=(print if args.verbose else None),
                               strategy_cls=strategy_cls, cost_pts=cost,
                               point_value=sym["point_value"])
        # the v1/v2 config presets only shape the lrev gates - stamping them
        # on other engines' reports is misleading
        label = args.config if args.engine == "lrev" else args.engine
        print_report(broker, point_value=sym["point_value"],
                     title=f"BACKTEST RESULT  [{sym['name']} {label}]  "
                           f"{t0.date()} .. {t1.date()}")
        if csv_path:
            print("\ntrade log:", csv_path)
        port_dfs.append((sym["name"], pd.DataFrame(broker.closed)))

    if not port_dfs:
        raise SystemExit("no symbol produced results")
    print_portfolio(port_dfs)   # prints only when >= 2 symbols have trades


if __name__ == "__main__":
    main()
