"""Build the backtest cache from the raw Databento DBN files.

Reads (from the repo's Data/ folder, or $LCB_RAW):
    *ohlcv1m*.dbn.zst   GC parent OHLCV-1m (all contracts, warmup history)
    *tbbo*.dbn.zst      GC continuous front-month TBBO (trades + BBO)

Writes (to data_cache/, or $LCB_CACHE):
    segments.json                    front-month contract windows (from DBN metadata)
    seg/<SYM>_{M15,H1,H4}.parquet    bars per contract incl. warmup
    seg/<SYM>_tbbo.parquet           TBBO ticks per contract window

Run once after downloading new data:  python3 prep.py
Requires:  pip install databento pandas pyarrow zstandard
"""
import glob
import json
import os

import numpy as np
import pandas as pd

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.environ.get("LCB_RAW", os.path.join(_REPO, "Data"))
CACHE = os.environ.get("LCB_CACHE", os.path.join(_REPO, "data_cache"))


def find_one(pattern):
    hits = sorted(glob.glob(os.path.join(RAW, pattern)))
    if not hits:
        raise SystemExit(f"no file matching {pattern} in {RAW}")
    if len(hits) > 1:
        print(f"note: multiple matches for {pattern}, using {hits[-1]}")
    return hits[-1]


def decode_tbbo(path):
    """Stream-decode TBBO DBN -> DataFrame + front-month segment table."""
    from databento_dbn import DBNDecoder
    import zstandard as zstd
    import databento as db

    meta = db.DBNStore.from_file(path).metadata
    segments = []
    for sym, ivals in meta.mappings.items():
        for iv in ivals:
            segments.append({"iid": int(iv["symbol"]),
                             "start": str(iv["start_date"]),
                             "end": str(iv["end_date"])})
    segments.sort(key=lambda s: s["start"])

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
    return df, segments


def decode_ohlcv1m(path):
    import databento as db
    store = db.DBNStore.from_file(path)
    df = store.to_df()
    return df[~df["symbol"].str.contains("-")]  # outrights only


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
    os.makedirs(os.path.join(CACHE, "seg"), exist_ok=True)

    tbbo_path = find_one("*tbbo*.dbn.zst")
    m1_path = find_one("*ohlcv1m*.dbn.zst")
    print("decoding", tbbo_path)
    tb, segments = decode_tbbo(tbbo_path)
    print(f"  {len(tb):,} TBBO records, {len(segments)} contract windows")
    print("decoding", m1_path)
    m1all = decode_ohlcv1m(m1_path).reset_index()

    iid2sym = (m1all.drop_duplicates("instrument_id")
               .set_index("instrument_id")["symbol"].to_dict())
    for s in segments:
        s["symbol"] = str(iid2sym.get(s["iid"], s["iid"]))

    with open(os.path.join(CACHE, "segments.json"), "w") as f:
        json.dump(segments, f, indent=2)

    for s in segments:
        sym, iid = s["symbol"], s["iid"]
        m1 = (m1all[m1all["instrument_id"] == iid]
              .set_index("ts_event").sort_index()
              [["open", "high", "low", "close", "volume"]])
        m1 = m1[:pd.Timestamp(s["end"], tz="UTC")]  # warmup + window
        for tfmin, name in [(15, "M15"), (60, "H1"), (240, "H4")]:
            agg_bars(m1, tfmin).to_parquet(
                os.path.join(CACHE, "seg", f"{sym}_{name}.parquet"))
        seg_tb = tb[tb["iid"] == iid].sort_values("ts").reset_index(drop=True)
        seg_tb.to_parquet(os.path.join(CACHE, "seg", f"{sym}_tbbo.parquet"))
        print(f"  {sym}: {len(m1):,} m1 bars, {len(seg_tb):,} ticks "
              f"({s['start']} .. {s['end']})")
    print("cache ready:", CACHE)


if __name__ == "__main__":
    main()
