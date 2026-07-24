"""Download GC/SI/HG/... history from Databento into Data/<SYMBOL>/.

    export DATABENTO_API_KEY=db-XXXX   (or config.py)
    python3 scripts/download_data.py --symbol GC --start 2026-07-18 --end 2026-09-01
    python3 scripts/download_data.py --symbol SI --start 2025-01-01 --end 2026-07-01

Downloads the two schemas the backtest needs (continuous front-month TBBO +
parent OHLCV-1m), then run:  python3 scripts/prep.py --symbol <SYMBOL>
Cost note: TBBO runs millions of records/month for liquid symbols; check
your Databento usage page if unsure.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import databento as db

from lrev.symbols import get_symbol

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_BASE = os.environ.get("LCB_RAW", os.path.join(_REPO, "Data"))


def get_api_key():
    try:
        import config
        key = getattr(config, "DATABENTO_API_KEY", "") or ""
        if key.startswith("db-"):
            return key
    except ImportError:
        pass
    return os.environ.get("DATABENTO_API_KEY", "") or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GC")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    args = ap.parse_args()
    sym = get_symbol(args.symbol)
    raw = os.path.join(RAW_BASE, sym["name"])
    os.makedirs(raw, exist_ok=True)

    hist = db.Historical(get_api_key())
    tag = sym["name"].lower()
    jobs = [
        ("tbbo", "continuous", [sym["continuous"]],
         f"{tag}_tbbo_v0_{args.start}_{args.end}.dbn.zst"),
        ("ohlcv-1m", "parent", [sym["parent"]],
         f"{tag}_ohlcv1m_parent_{args.start}_{args.end}.dbn.zst"),
    ]
    for schema, stype, symbols, fname in jobs:
        out = os.path.join(raw, fname)
        print(f"downloading {schema} ({symbols}) {args.start}..{args.end} -> {out}")
        data = hist.timeseries.get_range(
            dataset=sym["dataset"], schema=schema, stype_in=stype,
            symbols=symbols, start=args.start, end=args.end)
        data.to_file(out)
        print("  done,", os.path.getsize(out) // 1_000_000, "MB")
    print(f"\nnow run:  python3 scripts/prep.py --symbol {sym['name']}")


if __name__ == "__main__":
    main()
