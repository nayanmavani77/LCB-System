"""Live data watcher: stream any Databento schema for any symbol and print
a human-readable tape with session-anchored CUMULATIVE DELTA (CVD).

    python run/watch.py                                   # GC, TBBO tape
    python run/watch.py --symbol SI --schema trades
    python run/watch.py --schema mbp-10                   # 10-level book watch
    python run/watch.py --schema mbo --quiet              # order-flow counters only
    python run/watch.py --min-size 10                     # big prints only

What you see:
  - every trade: time, side, size, price, running CVD (buy vol - sell vol,
    reset at the DST-aware CME session boundary, 17:00 ET), plus best
    bid/ask when the schema carries a book
  - BIG-TRADE ALERTS (shown even with --quiet): single prints AND sweeps -
    consecutive same-side fills within --sweep-ms are clustered into one
    aggressor order and judged by their SUM. Big = >= --big-mult x the
    rolling average trade size, with an absolute floor of --big-min.
    e.g.  >>> BIG SELL sweep 142 @ 4052.10->4051.60 in 38ms (n=14, 31x avg)
  - book schemas (mbp-1 / mbp-10): a top-of-book / depth snapshot at most
    once per --book-secs; mbp-10 shows total bid vs ask depth + imbalance
  - mbo: per-second add/cancel/modify counters (plus every trade)
  - every minute: [1m] summary - OHLC, volume, minute delta, CVD, counts
  - Ctrl-C: session totals

Schema availability depends on your Databento license (GLBX.MDP3 carries
tbbo, trades, mbp-1, mbp-10 and mbo). Watching is read-only - no orders,
no strategy, safe to run alongside live.py.
NOTE on MBO sides: for non-trade events the side is the RESTING order's
side (an 'A'-side add is someone quoting the ask, not a seller hitting the
bid) - only 'T' events contribute to CVD, where side = the aggressor.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_UNDEF = 2**63 - 1


def get_api_key():
    """Same lookup as run/live.py: config.py first, then the env var."""
    try:
        import config
        key = getattr(config, "DATABENTO_API_KEY", "") or ""
        if key.startswith("db-"):
            return key
    except ImportError:
        pass
    key = os.environ.get("DATABENTO_API_KEY", "")
    if not key.startswith("db-"):
        raise SystemExit(
            "No Databento API key found.\n"
            "Either create config.py with your key (template: docs/COMMANDS.md,"
            " section 1),\nor set the DATABENTO_API_KEY environment variable.")
    return key


def _px(raw):
    """Databento fixed-precision int -> float, None if undefined."""
    if raw is None or raw == _UNDEF:
        return None
    return raw / 1e9


def _hms(ts_ns):
    return time.strftime("%H:%M:%S", time.gmtime(ts_ns / 1e9)) + \
        f".{int(ts_ns / 1e6) % 1000:03d}"


class Tape:
    """Processes records from ANY of the supported schemas and prints the
    tape. Separated from the network loop so it is unit-testable."""

    def __init__(self, schema, out=print, min_size=0.0, quiet=False,
                 book_secs=1.0, clock=time.monotonic,
                 big_mult=10.0, big_min=20.0, sweep_ms=50.0):
        from engines.gtrend import _session_end_ns
        self._session_end_ns = _session_end_ns
        self.schema = schema
        self.out = out
        self.min_size = min_size
        self.quiet = quiet
        self.book_secs = book_secs
        self.clock = clock
        # big-trade / sweep detection
        self.big_mult = big_mult       # big if >= this x rolling avg size...
        self.big_min = big_min         # ...and always at least this many
        self.sweep_ns = sweep_ms * 1e6
        self.avg_size = None           # EMA of trade sizes
        self._cl = None                # running same-side sweep cluster
        self.big = dict(nb=0, vb=0.0, ns=0, vs=0.0)   # session big counters
        # session state
        self.sess_end = None
        self.cvd = 0.0
        self.sess_vol = 0.0
        self.sess_trades = 0
        # current minute bar
        self.m_t0 = None
        self.m = None                    # dict(o,h,l,c,vol,delta,n)
        self.counts = dict(A=0, C=0, M=0)   # mbo adds/cancels/modifies
        self._last_book = -1e9
        self.bid = self.ask = None

    # ------------------------------------------------------------- events
    def on_record(self, rec):
        ts = getattr(rec, "ts_recv", None) or getattr(rec, "ts_event", None)
        if ts is None:
            return
        if self.sess_end is None or ts >= self.sess_end:
            if self.sess_end is not None:
                self._flush_cluster()
                self._session_close()
            self.sess_end = self._session_end_ns(ts)
            self.cvd = self.sess_vol = 0.0
            self.sess_trades = 0
            self.big = dict(nb=0, vb=0.0, ns=0, vs=0.0)
            self.out(f"--- new CME session (CVD reset) ---")

        # book levels, if the schema carries them
        levels = getattr(rec, "levels", None)
        if levels:
            l0 = levels[0]
            self.bid = _px(getattr(l0, "bid_px", None))
            self.ask = _px(getattr(l0, "ask_px", None))

        action = getattr(rec, "action", "T")
        price = _px(getattr(rec, "price", None))
        size = getattr(rec, "size", 0) or 0
        side = getattr(rec, "side", "N")

        if action == "T" and price is not None and size > 0:
            self._trade(ts, price, size, side)
        elif action in self.counts:
            self.counts[action] += 1
        # throttled book snapshot for book schemas
        if not self.quiet and self.schema in ("mbp-1", "mbp-10") \
                and action != "T":
            now = self.clock()
            if now - self._last_book >= self.book_secs:
                self._last_book = now
                self._book_line(levels)

    def _trade(self, ts, px, size, side):
        sgn = 1 if side == "B" else (-1 if side == "A" else 0)
        # sweep clustering: consecutive same-side fills within sweep_ms are
        # ONE aggressor order eating the book - judge the SUM, not the parts
        if self._cl is not None and (side != self._cl["side"]
                                     or ts - self._cl["t1"] > self.sweep_ns):
            self._flush_cluster()
        if sgn != 0:
            if self._cl is None:
                self._cl = dict(side=side, t0=ts, t1=ts, size=0.0,
                                px0=px, px1=px, n=0)
            self._cl["t1"] = ts
            self._cl["px1"] = px
            self._cl["size"] += size
            self._cl["n"] += 1
        # rolling average trade size (EMA, slow)
        self.avg_size = (size if self.avg_size is None
                         else self.avg_size * 0.99 + size * 0.01)
        self.cvd += sgn * size
        self.sess_vol += size
        self.sess_trades += 1
        # minute bar
        t0 = ts - ts % (60 * 10**9)
        if self.m_t0 is None or t0 != self.m_t0:
            if self.m_t0 is not None:
                self._minute_line()
            self.m_t0 = t0
            self.m = dict(o=px, h=px, l=px, c=px, vol=0.0, delta=0.0, n=0)
        self.m["h"] = max(self.m["h"], px)
        self.m["l"] = min(self.m["l"], px)
        self.m["c"] = px
        self.m["vol"] += size
        self.m["delta"] += sgn * size
        self.m["n"] += 1
        if self.quiet or size < self.min_size:
            return
        tag = "BUY " if sgn > 0 else ("SELL" if sgn < 0 else " ?  ")
        book = (f"  [{self.bid:.2f}/{self.ask:.2f}]"
                if self.bid is not None and self.ask is not None else "")
        self.out(f"{_hms(ts)}  {tag} {size:>6.0f} @ {px:<9.2f} "
                 f"CVD {self.cvd:+10,.0f}{book}")

    def _flush_cluster(self):
        """Close the running sweep cluster; alert if it was BIG. Alerts
        print even in --quiet mode - they are the point of watching."""
        cl, self._cl = self._cl, None
        if cl is None:
            return
        thr = max(self.big_min,
                  (self.avg_size or 0.0) * self.big_mult)
        if cl["size"] < thr:
            return
        buy = cl["side"] == "B"
        if buy:
            self.big["nb"] += 1
            self.big["vb"] += cl["size"]
        else:
            self.big["ns"] += 1
            self.big["vs"] += cl["size"]
        mult = (f", {cl['size'] / self.avg_size:.0f}x avg"
                if self.avg_size else "")
        px = (f"{cl['px0']:.2f}" if cl["n"] == 1
              else f"{cl['px0']:.2f}->{cl['px1']:.2f}")
        dur = (f" in {(cl['t1'] - cl['t0']) / 1e6:.0f}ms"
               if cl["n"] > 1 else "")
        kind = "sweep" if cl["n"] > 1 else "print"
        self.out(f">>> BIG {'BUY ' if buy else 'SELL'} {kind} "
                 f"{cl['size']:,.0f} @ {px}{dur} "
                 f"(n={cl['n']}{mult})  CVD {self.cvd:+,.0f}")

    # ------------------------------------------------------------- output
    def _book_line(self, levels):
        if not levels:
            return
        if self.schema == "mbp-1" or len(levels) == 1:
            l0 = levels[0]
            bs = getattr(l0, "bid_sz", 0)
            azz = getattr(l0, "ask_sz", 0)
            if self.bid is None or self.ask is None:
                return
            self.out(f"          book  {bs:>5} x {self.bid:.2f} | "
                     f"{self.ask:.2f} x {azz:<5}  CVD {self.cvd:+,.0f}")
            return
        bid_d = sum(getattr(x, "bid_sz", 0) or 0 for x in levels)
        ask_d = sum(getattr(x, "ask_sz", 0) or 0 for x in levels)
        tot = bid_d + ask_d
        imb = (bid_d - ask_d) / tot if tot else 0.0
        self.out(f"          depth{len(levels)}  bid {bid_d:>6} | "
                 f"ask {ask_d:<6}  imb {imb:+.0%}  "
                 f"[{self.bid:.2f}/{self.ask:.2f}]  CVD {self.cvd:+,.0f}")

    def _minute_line(self):
        m = self.m
        t = time.strftime("%H:%M", time.gmtime(self.m_t0 / 1e9))
        extra = ""
        if self.schema == "mbo":
            extra = (f" | +{self.counts['A']} adds "
                     f"-{self.counts['C']} cxl ~{self.counts['M']} mod")
            self.counts = dict(A=0, C=0, M=0)
        b = self.big
        bigs = (f" | big {b['nb']}B/{b['ns']}S {b['vb'] - b['vs']:+,.0f}"
                if b["nb"] or b["ns"] else "")
        self.out(f"[1m {t}] O {m['o']:.2f} H {m['h']:.2f} L {m['l']:.2f} "
                 f"C {m['c']:.2f} | vol {m['vol']:,.0f} "
                 f"delta {m['delta']:+,.0f} | CVD {self.cvd:+,.0f} | "
                 f"{m['n']} trades{bigs}{extra}")

    def _session_close(self):
        b = self.big
        self.out(f"=== session done: vol {self.sess_vol:,.0f}, "
                 f"CVD {self.cvd:+,.0f}, {self.sess_trades} trades | "
                 f"big: {b['nb']} buys {b['vb']:+,.0f} / "
                 f"{b['ns']} sells {-b['vs']:+,.0f} ===")

    def summary(self):
        self._flush_cluster()
        if self.m_t0 is not None:
            self._minute_line()
        self._session_close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default="GC",
                    help="symbol from core/symbols.py (default GC)")
    ap.add_argument("--schema", default="tbbo",
                    choices=["tbbo", "trades", "mbp-1", "mbp-10", "mbo"],
                    help="Databento schema to stream (default tbbo)")
    ap.add_argument("--min-size", type=float, default=0,
                    help="print only trades of at least this size (contracts)")
    ap.add_argument("--quiet", action="store_true",
                    help="no tape - only the [1m] summary lines")
    ap.add_argument("--book-secs", type=float, default=1.0,
                    help="min seconds between book snapshots (mbp schemas)")
    ap.add_argument("--big-mult", type=float, default=10.0,
                    help="BIG alert when a print/sweep >= this x the rolling "
                         "average trade size (default 10)")
    ap.add_argument("--big-min", type=float, default=20.0,
                    help="absolute floor for a BIG alert in contracts "
                         "(default 20)")
    ap.add_argument("--sweep-ms", type=float, default=50.0,
                    help="same-side fills within this many ms cluster into "
                         "one sweep (default 50)")
    args = ap.parse_args()

    import databento as db

    from core.symbols import get_symbol
    sym = get_symbol(args.symbol)
    key = get_api_key()
    tape = Tape(args.schema, min_size=args.min_size, quiet=args.quiet,
                book_secs=args.book_secs, big_mult=args.big_mult,
                big_min=args.big_min, sweep_ms=args.sweep_ms)
    print(f"watching {sym['name']} ({sym['continuous']}) schema={args.schema}"
          f" | CVD resets at the CME session boundary | Ctrl-C to stop")
    while True:
        client = db.Live(key=key)
        client.subscribe(dataset=sym["dataset"], schema=args.schema,
                         stype_in="continuous", symbols=[sym["continuous"]])
        try:
            for rec in client:
                tape.on_record(rec)
        except KeyboardInterrupt:
            tape.summary()
            try:
                client.stop()
            except Exception:
                pass
            return
        except Exception as exc:
            print(f"[reconnect] stream lost: {exc}; retrying in 10s "
                  f"(CVD continues) - Ctrl-C to stop")
            try:
                client.stop()
            except Exception:
                pass
            try:
                time.sleep(10)
            except KeyboardInterrupt:
                tape.summary()
                return


if __name__ == "__main__":
    main()
