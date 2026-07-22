"""Prepare per-segment data: bars (M15/H1/H4) per active contract + TBBO arrays."""
import pandas as pd
import numpy as np
import os

DATA = "/home/claude/lcb/data"

SEGMENTS = [
    # (symbol, iid, seg_start, seg_end)  -- TBBO active window (UTC)
    ("GCG6", 42001025, "2025-12-28", "2026-01-30"),
    ("GCJ6", 42000890, "2026-01-30", "2026-03-30"),
    ("GCM6", 19181,    "2026-03-30", "2026-05-29"),
    ("GCQ6", 42011464, "2026-05-29", "2026-07-18"),
]

def agg_bars(m1, minutes):
    """Aggregate 1m bars (indexed by open-time ts_event) to N-minute bars."""
    rule = f"{minutes}min"
    o = m1['open'].resample(rule).first()
    h = m1['high'].resample(rule).max()
    l = m1['low'].resample(rule).min()
    c = m1['close'].resample(rule).last()
    v = m1['volume'].resample(rule).sum()
    out = pd.DataFrame({'open':o,'high':h,'low':l,'close':c,'volume':v}).dropna(subset=['open'])
    return out

def main():
    m1all = pd.read_parquet(f"{DATA}/ohlcv1m_outrights.parquet")
    m1all = m1all.reset_index()
    tb = pd.read_parquet(f"{DATA}/tbbo.parquet")

    os.makedirs(f"{DATA}/seg", exist_ok=True)
    for sym, iid, s, e in SEGMENTS:
        m1 = m1all[m1all['instrument_id']==iid].set_index('ts_event').sort_index()
        m1 = m1[['open','high','low','close','volume']]
        # keep warmup: everything up to seg end
        m1 = m1[:pd.Timestamp(e, tz='UTC')]
        for tfmin, name in [(15,'M15'),(60,'H1'),(240,'H4')]:
            bars = agg_bars(m1, tfmin)
            bars.to_parquet(f"{DATA}/seg/{sym}_{name}.parquet")
        seg_tb = tb[tb['iid']==iid].sort_values('ts').reset_index(drop=True)
        seg_tb.to_parquet(f"{DATA}/seg/{sym}_tbbo.parquet")
        print(sym, "m1", len(m1), "tbbo", len(seg_tb),
              "tb range", pd.Timestamp(seg_tb['ts'].min()), pd.Timestamp(seg_tb['ts'].max()))

if __name__ == "__main__":
    main()
