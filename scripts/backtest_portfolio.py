"""Portfolio backtest - run the engine over MULTIPLE symbols and combine in $.

    python scripts/backtest_portfolio.py                          # all cached symbols
    python scripts/backtest_portfolio.py --symbols GC,SI --start 2025-01-01
    python scripts/backtest_portfolio.py --symbols GC,SI --rr 3.0 --engine ldef

Each symbol replays through the same engine with its own cache, cost and
point value (from lrev/symbols.py). Results combine in DOLLARS (points are
not comparable across symbols: 1 SI point = $5,000, 1 GC point = $100).
The combined max drawdown uses the merged, time-ordered trade stream - this
is where correlated symbols (GC+SI+PL together) show their true joint risk.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from lrev.cli import add_strategy_args, config_from_args, describe
from lrev.data import cache_for, data_bounds, replay_window
from lrev.symbols import SYMBOLS, get_symbol

CONFIGS = {
    "v2-flow": {"use_flow_gate": True},
    "v2-ea": {"use_flow_gate": False},
    "v1": {"use_flow_gate": False, "order_max_age_h": 0, "max_spread": 0.0},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None,
                    help="comma list, e.g. GC,SI (default: every symbol "
                         "that has a built cache)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--config", default="v2-flow", choices=sorted(CONFIGS))
    ap.add_argument("--engine", default="lrev", choices=["lrev", "ldef"])
    add_strategy_args(ap)
    args = ap.parse_args()

    if args.symbols:
        names = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        names = [s for s in sorted(SYMBOLS)
                 if os.path.exists(os.path.join(cache_for(s), "segments.json"))]
    if not names:
        raise SystemExit("no symbol caches found - run scripts/prep.py first")

    all_trades = []
    summary_rows = []
    for name in names:
        sym = get_symbol(name)
        cache = cache_for(name)
        if not os.path.exists(os.path.join(cache, "segments.json")):
            print(f"-- {name}: no cache, skipped "
                  f"(python scripts/prep.py --symbol {name})")
            continue
        cfg = config_from_args(args, base=CONFIGS[args.config])
        if args.max_spread is None:
            cfg["max_spread"] = sym["max_spread"]
        strategy_cls = None
        if args.engine == "ldef":
            from lrev.defend import DEFEND_CONFIG, LDefStrategy
            strategy_cls = LDefStrategy
            if args.flow_lo is None:
                cfg["flow_lo"] = DEFEND_CONFIG["flow_lo"]
            if args.flow_hi is None:
                cfg["flow_hi"] = DEFEND_CONFIG["flow_hi"]
        print(f"\n=== {name} ===  {describe(cfg)}")
        broker = replay_window(start=args.start, end=args.end, config=cfg,
                               cache=cache, strategy_cls=strategy_cls,
                               cost_pts=sym["cost_pts"],
                               point_value=sym["point_value"])
        df = pd.DataFrame(broker.closed)
        if df.empty:
            print(f"  {name}: no trades")
            continue
        df["symbol"] = name
        all_trades.append(df)
        pts = df["pnl_pts"]
        usd = df["pnl_usd"]
        pf = usd[usd > 0].sum() / max(1e-9, -usd[usd < 0].sum())
        summary_rows.append({
            "symbol": name, "trades": len(df),
            "net_usd": round(usd.sum(), 0),
            "avg_usd": round(usd.mean(), 1),
            "win%": round(100 * (usd > 0).mean(), 1),
            "pf": round(pf, 2),
        })
        print(f"  {name}: {len(df)} trades, net ${usd.sum():+,.0f}, PF {pf:.2f}")

    if not all_trades:
        raise SystemExit("no trades anywhere")

    print("\n" + "=" * 58)
    print("  PORTFOLIO RESULT (all symbols combined, in $)")
    print("=" * 58)
    print("  " + pd.DataFrame(summary_rows).to_string(index=False)
          .replace("\n", "\n  "))

    port = (pd.concat(all_trades, ignore_index=True)
            .sort_values("ts_close").reset_index(drop=True))
    usd = port["pnl_usd"]
    eq = usd.cumsum()
    dd = (eq.cummax() - eq).max()
    pf = usd[usd > 0].sum() / max(1e-9, -usd[usd < 0].sum())
    print(f"\n  combined trades : {len(port)}")
    print(f"  combined net    : ${usd.sum():+,.0f}")
    print(f"  combined PF     : {pf:.2f}")
    print(f"  combined max DD : ${dd:,.0f}   <- joint risk of running "
          f"these symbols together")
    port["month"] = (pd.to_datetime(port["ts_close"], utc=True)
                     .dt.tz_convert(None).dt.to_period("M"))
    print("\n  -- combined monthly ($) " + "-" * 24)
    m = port.groupby("month")["pnl_usd"].agg(n="size", usd="sum").round(0)
    m["cum_usd"] = m["usd"].cumsum()
    print("  " + m.to_string().replace("\n", "\n  "))
    print("=" * 58)


if __name__ == "__main__":
    main()
