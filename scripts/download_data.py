"""Extend Data/ with fresh GC history from Databento (historical API).

    export DATABENTO_API_KEY=db-XXXX
    python3 download_data.py --start 2026-07-18 --end 2026-09-01

Downloads the same two schemas the backtest needs (continuous front-month
TBBO + parent OHLCV-1m) into the repo's Data/ folder, then re-run
backtest/prep.py to rebuild the cache. Cost note: TBBO for GC runs roughly
2-3M records/month; check the print-out of hist.metadata.get_cost if unsure.
"""
import argparse
import os

import databento as db

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.environ.get("LCB_RAW", os.path.join(_REPO, "Data"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    args = ap.parse_args()

    hist = db.Historical()
    jobs = [
        ("tbbo", "continuous", ["GC.v.0"], f"gc_tbbo_v0_{args.start}_{args.end}.dbn.zst"),
        ("ohlcv-1m", "parent", ["GC.FUT"], f"gc_ohlcv1m_parent_{args.start}_{args.end}.dbn.zst"),
    ]
    os.makedirs(RAW, exist_ok=True)
    for schema, stype, symbols, fname in jobs:
        out = os.path.join(RAW, fname)
        print(f"downloading {schema} ({symbols}) {args.start}..{args.end} -> {out}")
        data = hist.timeseries.get_range(
            dataset="GLBX.MDP3", schema=schema, stype_in=stype,
            symbols=symbols, start=args.start, end=args.end)
        data.to_file(out)
        print("  done,", os.path.getsize(out) // 1_000_000, "MB")
    print("\nnow run:  python3 backtest/prep.py")
    print("note: prep.py globs *tbbo*/*ohlcv1m* and uses the NEWEST match;")
    print("for a seamless extended backtest, download one range covering old+new dates.")


if __name__ == "__main__":
    main()
