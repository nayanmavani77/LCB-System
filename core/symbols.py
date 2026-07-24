"""Symbol registry - everything symbol-specific lives HERE, not in the engines.

The strategy engines (lrev/strategy.py, lrev/defend.py) are price-scale
agnostic and are NOT modified per symbol. What differs per symbol:

  dataset      Databento dataset (all CME metals/energy = GLBX.MDP3)
  continuous   volume-based front-month symbol for TBBO (X.v.0)
  parent       parent symbol for OHLCV warmup downloads (X.FUT)
  point_value  $ per 1.00 price move per futures contract (for reports)
  mt5_symbol   default MT5 CFD symbol to execute on (override with --mt5-symbol)
  mt5_lot_note lot-size equivalence caveat for the MT5 CFD
  max_spread   default spread gate in price units (tune per symbol!)
  cost_pts     default commission+slippage per round turn in price units

IMPORTANT: the strategy's gates and SL multipliers were RESEARCHED AND
VALIDATED ON GC ONLY. For any other symbol these defaults are reasonable
starting points, not validated settings - backtest first, always.

Add a new symbol by adding one dict entry.
"""
from __future__ import annotations

SYMBOLS = {
    "GC": {   # COMEX Gold - the validated original
        "dataset": "GLBX.MDP3", "continuous": "GC.v.0", "parent": "GC.FUT",
        "point_value": 100.0,          # 100 oz x $1
        "mt5_symbol": "XAUUSD",
        "mt5_lot_note": "1.00 lot XAUUSD = 100 oz = 1 GC contract",
        "max_spread": 0.90, "cost_pts": 0.40,
    },
    "SI": {   # COMEX Silver
        "dataset": "GLBX.MDP3", "continuous": "SI.v.0", "parent": "SI.FUT",
        "point_value": 5000.0,         # 5,000 oz x $1
        "mt5_symbol": "XAGUSD",
        "mt5_lot_note": "1.00 lot XAGUSD = 5,000 oz = 1 SI contract (check broker)",
        "max_spread": 0.03, "cost_pts": 0.015,
    },
    "HG": {   # COMEX Copper ($/lb)
        "dataset": "GLBX.MDP3", "continuous": "HG.v.0", "parent": "HG.FUT",
        "point_value": 25000.0,        # 25,000 lb x $1
        "mt5_symbol": "COPPER",
        "mt5_lot_note": "CFD lot sizes vary by broker - verify $/point first",
        "max_spread": 0.01, "cost_pts": 0.004,
    },
    "PL": {   # NYMEX Platinum
        "dataset": "GLBX.MDP3", "continuous": "PL.v.0", "parent": "PL.FUT",
        "point_value": 50.0,           # 50 oz x $1
        "mt5_symbol": "XPTUSD",
        "mt5_lot_note": "CFD lot sizes vary by broker - verify $/point first",
        "max_spread": 2.0, "cost_pts": 0.8,
    },
    "CL": {   # NYMEX WTI Crude
        "dataset": "GLBX.MDP3", "continuous": "CL.v.0", "parent": "CL.FUT",
        "point_value": 1000.0,         # 1,000 bbl x $1
        "mt5_symbol": "USOIL",
        "mt5_lot_note": "CFD oil lots often 100 bbl (0.1x a CL contract) - verify",
        "max_spread": 0.05, "cost_pts": 0.02,
    },
    "NG": {   # NYMEX Natural Gas
        "dataset": "GLBX.MDP3", "continuous": "NG.v.0", "parent": "NG.FUT",
        "point_value": 10000.0,        # 10,000 MMBtu x $1
        "mt5_symbol": "NGAS",
        "mt5_lot_note": "CFD lot sizes vary widely - verify $/point first",
        "max_spread": 0.008, "cost_pts": 0.003,
    },
}


def get_symbol(name: str) -> dict:
    key = name.upper()
    if key not in SYMBOLS:
        raise SystemExit(
            f"unknown symbol '{name}'. Known: {', '.join(sorted(SYMBOLS))}. "
            f"Add new symbols in lrev/symbols.py (one dict entry).")
    return dict(SYMBOLS[key], name=key)
