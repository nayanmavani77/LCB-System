"""Build the backtest cache from raw Databento DBN files (multi-year aware).

Reads ALL matching files from the repo's Data/ folder (or $LCB_RAW):
    *ohlcv1m*.dbn.zst   GC parent OHLCV-1m (any number of files, e.g. per year)
    *tbbo*.dbn.zst      GC continuous front-month TBBO (any number of files)

Front-month contract windows are read from each TBBO file's metadata and
MERGED across files (a contract that rolls near a year boundary appears at
the end of one file and the start of the next - it becomes one segment).

Writes (to data_cache/, or $LCB_CACHE):
    segments.json                    merged contract windows
    seg/<SYM>_{M15,H1,H4}.parquet    bars per contract (incl. 30d warmup)
    seg/<SYM>_tbbo.parquet           TBBO ticks per contract window

Run once after adding data:  python3 scripts/prep.py
Requires:  pip install databento pandas pyarrow zstandard
Memory note: files are processed one at a time; peak usage is roughly one
year of TBBO as a DataFrame (~2-3 GB for a busy year).
"""
import gc
import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_BASE = os.environ.get("LCB_RAW", os.path.join(_REPO, "Data"))
CACHE_BASE = os.environ.get("LCB_CACHE", os.path.join(_REPO, "data_cache"))
WARMUP_DAYS = 30

# set by main() per --symbol; GC falls back to the legacy flat Data/ layout
RAW = RAW_BASE
CACHE = CACHE_BASE


def resolve_dirs(symbol: str):
    global RAW, CACHE
    sym = symbol.upper()
    per_raw = os.path.join(RAW_BASE, sym)
    if os.path.isdir(per_raw) and glob.glob(os.path.join(per_raw, "*.dbn.zst")):
        RAW = per_raw
    elif sym == "GC" and glob.glob(os.path.join(RAW_BASE, "*.dbn.zst")):
        RAW = RAW_BASE          # legacy flat layout for GC
    else:
        RAW = per_raw           # will raise a clear error in find_all
    CACHE = os.path.join(CACHE_BASE, sym)
    if sym == "GC" and not os.path.isdir(CACHE) \
            and os.path.exists(os.path.join(CACHE_BASE, "segments.json")):
        CACHE = CACHE_BASE      # keep updating the legacy GC cache


def find_all(pattern):
    hits = sorted(glob.glob(os.path.join(RAW, pattern)))
    if not hits:
        raise SystemExit(f"no file matching {pattern} in {RAW}")
    return hits


def read_mappings(path):
    """Contract windows from one TBBO file's metadata (no full decode)."""
    import databento as db
    meta = db.DBNStore.from_file(path).metadata
    out = []
    for _, ivals in meta.mappings.items():
        for iv in ivals:
            out.append({"iid": int(iv["symbol"]),
                        "start": str(iv["start_date"]),
                        "end": str(iv["end_date"])})
    return out


def merge_segments(per_file_maps, max_gap_days=5):
    """Merge windows of the same instrument across files. Tolerates small
    gaps: metadata windows are sometimes clipped exactly at a file's query
    boundary (e.g. one file ends a contract 2025-12-31, the next starts it
    2026-01-01), so same-instrument windows within max_gap_days join."""
    from datetime import date, timedelta

    ivals = sorted((iv for m in per_file_maps for iv in m),
                   key=lambda x: (x["start"], x["end"]))
    merged = []
    for iv in ivals:
        target = None
        for m in reversed(merged):
            if m["iid"] == iv["iid"]:
                target = m
                break
        if target is not None:
            gap_ok = (date.fromisoformat(iv["start"]) <=
                      date.fromisoformat(target["end"]) +
                      timedelta(days=max_gap_days))
            if gap_ok:
                target["end"] = max(target["end"], iv["end"])
                continue
        merged.append(dict(iv))
    return merged


def decode_tbbo_records(path):
    from databento_dbn import DBNDecoder
    import zstandard as zstd

    dctx = zstd.ZstdDecompressor()
    dec = DBNDecoder()
    L = {k: [] for k in ["ts", "price", "size", "side", "bid", "ask",
                         "bidsz", "asksz", "iid"]}
    with open(path, "rb") as f, dctx.stream_reader(f) as r:
        while True:
            chunk = r.read(1 << 22)
            if not chunk:
                break
            dec.write(chunk)
            for rec in dec.decode():
                if type(rec).__name__ != "MBP1Msg":
                    continue
                L["ts"].append(rec.ts_recv)
                L["price"].append(rec.price)
                L["size"].append(rec.size)
                s = rec.side
                L["side"].append(ord(s) if isinstance(s, str) else s)
                L["bid"].append(rec.bid_px_00)
                L["ask"].append(rec.ask_px_00)
                L["bidsz"].append(rec.bid_sz_00)
                L["asksz"].append(rec.ask_sz_00)
                L["iid"].append(rec.instrument_id)
    df = pd.DataFrame({
        "ts": np.array(L["ts"], dtype="int64"),
        "price": np.array(L["price"], dtype="int64") / 1e9,
        "size": np.array(L["size"], dtype="int32"),
        "side": np.array(L["side"], dtype="uint8"),
        "bid": np.array(L["bid"], dtype="int64") / 1e9,
        "ask": np.array(L["ask"], dtype="int64") / 1e9,
        "bidsz": np.array(L["bidsz"], dtype="int32"),
        "asksz": np.array(L["asksz"], dtype="int32"),
        "iid": np.array(L["iid"], dtype="int64"),
    })
    return _drop_invalid_quotes(df, path)


def _drop_invalid_quotes(df, path=""):
    """Remove ticks whose quote is not a real two-sided market.

    Databento encodes an UNDEFINED price as INT64_MAX, which becomes
    ~9.223e9 after the 1e-9 scaling. Left in the cache such a row does real
    damage: the PaperBroker fills against it (a fictional multi-billion
    dollar trade in the equity curve) and any spread gate reading
    ask - bid on it produces a number no cap can be compared against
    meaningfully. Crossed and non-positive books are dropped for the same
    reason. The count is PRINTED, not swallowed, so a tape with a lot of
    them is visible rather than quietly cleaned."""
    LIMIT = 1e7                     # $10m/oz - anything above is an encoding
    ok = ((df.price > 0) & (df.price < LIMIT)
          & (df.bid > 0) & (df.bid < LIMIT)
          & (df.ask > 0) & (df.ask < LIMIT)
          & (df.ask >= df.bid))
    n_bad = int((~ok).sum())
    if n_bad:
        print(f"    dropped {n_bad:,} of {len(df):,} ticks with invalid "
              f"quotes (undefined/crossed/non-positive)"
              f"{' in ' + os.path.basename(path) if path else ''}")
    return df[ok].reset_index(drop=True)


def agg_bars(m1, minutes):
    rule = f"{minutes}min"
    out = pd.DataFrame({
        "open": m1["open"].resample(rule).first(),
        "high": m1["high"].resample(rule).max(),
        "low": m1["low"].resample(rule).min(),
        "close": m1["close"].resample(rule).last(),
        "volume": m1["volume"].resample(rule).sum(),
    }).dropna(subset=["open"])
    return out


def main():
    import databento as db

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GC",
                    help="symbol short name from lrev/symbols.py (default GC); "
                         "expects DBN files in Data/<SYMBOL>/ (GC also: Data/)")
    args = ap.parse_args()
    resolve_dirs(args.symbol)
    print(f"symbol {args.symbol.upper()}: raw={RAW}  cache={CACHE}")

    os.makedirs(os.path.join(CACHE, "seg"), exist_ok=True)
    tbbo_files = find_all("*tbbo*.dbn.zst")
    m1_files = find_all("*ohlcv1m*.dbn.zst")
    print(f"{len(tbbo_files)} TBBO file(s), {len(m1_files)} OHLCV-1m file(s)")

    # ---- segments merged across all TBBO files
    segments = merge_segments([read_mappings(p) for p in tbbo_files])

    # ---- all 1m outright bars (small enough to hold fully)
    m1_parts = []
    for p in m1_files:
        df = db.DBNStore.from_file(p).to_df()
        m1_parts.append(df[~df["symbol"].str.contains("-")].reset_index())
    m1all = (pd.concat(m1_parts, ignore_index=True)
             .drop_duplicates(subset=["ts_event", "instrument_id"])
             .sort_values("ts_event"))
    del m1_parts
    gc.collect()

    # ---- symbol per segment, resolved INSIDE its own window
    #      (CME reuses instrument ids over the years - never match globally)
    for s in segments:
        t0 = pd.Timestamp(s["start"], tz="UTC")
        t1 = pd.Timestamp(s["end"], tz="UTC")
        rows = m1all[(m1all["instrument_id"] == s["iid"]) &
                     (m1all["ts_event"] >= t0 - pd.Timedelta(days=WARMUP_DAYS)) &
                     (m1all["ts_event"] < t1)]
        syms = rows["symbol"].unique()
        s["symbol"] = str(syms[0]) if len(syms) else str(s["iid"])
        if len(syms) > 1:
            print(f"WARNING: iid {s['iid']} maps to {list(syms)} in "
                  f"{s['start']}..{s['end']}; using {s['symbol']}")
    with open(os.path.join(CACHE, "segments.json"), "w") as f:
        json.dump(segments, f, indent=2)
    print(f"{len(segments)} segments: "
          f"{segments[0]['start']} .. {segments[-1]['end']}")

    # ---- bars per segment (window + 30d warmup, sliced time-aware by iid)
    for s in segments:
        t0 = pd.Timestamp(s["start"], tz="UTC") - pd.Timedelta(days=WARMUP_DAYS)
        t1 = pd.Timestamp(s["end"], tz="UTC")
        m1 = (m1all[(m1all["instrument_id"] == s["iid"]) &
                    (m1all["ts_event"] >= t0) & (m1all["ts_event"] < t1)]
              .set_index("ts_event").sort_index()
              [["open", "high", "low", "close", "volume"]])
        for tfmin, name in [(15, "M15"), (60, "H1"), (240, "H4")]:
            agg_bars(m1, tfmin).to_parquet(
                os.path.join(CACHE, "seg", f"{s['symbol']}_{name}.parquet"))

    # ---- ticks: one TBBO file at a time; flush a segment once fully covered
    # (bookkeeping keyed by segment index - names could theoretically collide)
    pending: dict = {i: [] for i in range(len(segments))}
    flushed: set = set()
    max_covered = ""
    for fi, path in enumerate(tbbo_files):
        print(f"decoding {os.path.basename(path)} ...")
        tb = decode_tbbo_records(path)
        print(f"  {len(tb):,} records")
        for i, s in enumerate(segments):
            if i in flushed:
                continue
            lo = pd.Timestamp(s["start"], tz="UTC").value
            hi = pd.Timestamp(s["end"], tz="UTC").value
            part = tb[(tb["iid"] == s["iid"]) &
                      (tb["ts"] >= lo) & (tb["ts"] < hi)]
            if len(part):
                pending[i].append(part.copy())
        del tb
        gc.collect()
        # everything ending on/before this file's last mapped date is complete
        file_end = max(iv["end"] for iv in read_mappings(path))
        max_covered = max(max_covered, file_end)
        is_last = fi == len(tbbo_files) - 1
        for i, s in enumerate(segments):
            if i in flushed:
                continue
            if is_last or s["end"] <= max_covered:
                flushed.add(i)
                parts = pending.pop(i)
                out = (pd.concat(parts, ignore_index=True)
                       .sort_values("ts").reset_index(drop=True)
                       if parts else pd.DataFrame())
                sym = s["symbol"]
                if len(out):
                    out.to_parquet(
                        os.path.join(CACHE, "seg", f"{sym}_tbbo.parquet"))
                    print(f"  {sym}: {len(out):,} ticks "
                          f"({s['start']} .. {s['end']})")
                else:
                    print(f"  {sym}: NO ticks in data - segment dropped")
                    s["empty"] = True
                del parts, out
                gc.collect()

    # drop segments that had no tick data (e.g. windows outside the files)
    segments = [s for s in segments if not s.get("empty")]
    with open(os.path.join(CACHE, "segments.json"), "w") as f:
        json.dump(segments, f, indent=2)
    print("cache ready:", CACHE)


if __name__ == "__main__":
    main()
