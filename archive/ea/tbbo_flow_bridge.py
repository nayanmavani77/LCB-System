"""TBBO flow bridge: Databento live GC feed -> 30s aggressor-imbalance file for MT5.

Writes one line to the MT5 Files folder every second:
    <unix_epoch_seconds> <imb_30s>
where imb_30s = (buy-aggressor volume - sell-aggressor volume) / total volume
over the trailing 30 seconds, computed from the GLBX.MDP3 TBBO (trades) stream
for the current front-month GC contract (continuous symbol GC.v.0).

The EA (L-Rev-System_TBBO_v2.mq5, L_UseFlowGate=true) reads this file at
trigger time and only takes a level break whose direction-aligned imbalance
is inside [L_FlowGateMin, L_FlowGateMax] (research band: [0.0, 0.6]).

Requirements:
    pip install databento
    export DATABENTO_API_KEY=db-XXXX...

Point OUT_PATH at your terminal's Files directory, e.g.
    C:/Users/<you>/AppData/Roaming/MetaQuotes/Terminal/<id>/MQL5/Files/lcb_flow.txt
or the Common\Files folder if the EA runs with FILE_COMMON.
"""
import collections
import os
import time

import databento as db

OUT_PATH = os.environ.get("LCB_FLOW_FILE", "lcb_flow.txt")
WINDOW_NS = 30 * 1_000_000_000  # 30 seconds


def main():
    client = db.Live()  # uses DATABENTO_API_KEY
    client.subscribe(
        dataset="GLBX.MDP3",
        schema="tbbo",
        stype_in="continuous",
        symbols=["GC.v.0"],
    )

    window = collections.deque()  # (ts_ns, signed_size)
    vol = 0
    signed = 0
    last_write = 0.0

    def write_line(now_s, imb):
        tmp = OUT_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{int(now_s)} {imb:.4f}\n")
        os.replace(tmp, OUT_PATH)

    for rec in client:
        if not hasattr(rec, "price") or rec.action != "T":
            continue
        side = getattr(rec, "side", "N")
        sgn = 1 if side == "B" else (-1 if side == "A" else 0)
        ts = rec.ts_recv
        window.append((ts, rec.size, sgn))
        vol += rec.size
        signed += rec.size * sgn
        # evict old
        while window and window[0][0] < ts - WINDOW_NS:
            ots, osz, osgn = window.popleft()
            vol -= osz
            signed -= osz * osgn
        now = time.time()
        if now - last_write >= 1.0:
            imb = (signed / vol) if vol > 0 else 0.0
            write_line(now, imb)
            last_write = now


if __name__ == "__main__":
    main()
