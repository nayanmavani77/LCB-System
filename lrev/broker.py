"""Broker interface + paper implementation for the L-Rev live engine.

To go live later, implement Broker for your venue (IBKR via ib_insync,
Tradovate, etc.) and pass it to LRevStrategy instead of PaperBroker.
The strategy only ever calls market_order(); SL/TP management can either
be delegated to the venue (bracket orders) or simulated with on_tick().
"""
from __future__ import annotations

import csv
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


class Broker(ABC):
    @abstractmethod
    def market_order(self, ts: int, direction: int, qty: float,
                     sl: float, tp: float, tag: str, ref_px: float = None):
        """direction: +1 buy / -1 sell. sl/tp are absolute prices on the
        SIGNAL instrument; ref_px is the engine's trigger price there.
        Adapters executing on a different instrument (e.g. XAUUSD from GC
        signals) should apply the DISTANCES (ref_px-sl, tp-ref_px) to their
        own instrument's current price - the futures/spot basis cancels."""

    def on_tick(self, ts: int, bid: float, ask: float):
        """Called on every tick. Default: no-op. PaperBroker overrides it to
        simulate SL/TP fills; real-venue adapters (server-side SL/TP) don't
        need it."""



@dataclass
class Position:
    ts_open: int
    direction: int
    qty: float
    entry: float
    sl: float
    tp: float
    tag: str


class PaperBroker(Broker):
    """Fills market orders at the current bid/ask, manages SL/TP on ticks,
    logs completed trades to CSV. Point value defaults to GC ($100/pt)."""

    def __init__(self, trade_log_path="paper_trades.csv", point_value=100.0,
                 cost_pts=0.4, log=print):
        self.positions: list[Position] = []
        self.closed = []
        self.bid = float("nan")
        self.ask = float("nan")
        self.point_value = point_value
        self.cost_pts = cost_pts
        self.log = log
        self.path = trade_log_path
        if self.path and not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(
                    ["ts_open", "ts_close", "dir", "qty", "entry", "exit",
                     "sl", "tp", "reason", "pnl_pts", "pnl_gross_pts",
                     "pnl_usd", "tag"])

    # ------------------------------------------------------------- interface
    def market_order(self, ts, direction, qty, sl, tp, tag, ref_px=None):
        px = self.ask if direction > 0 else self.bid
        pos = Position(ts, direction, qty, px, sl, tp, tag)
        self.positions.append(pos)
        self.log(f"FILL {'BUY' if direction>0 else 'SELL'} {qty} @ {px:.2f} "
                 f"SL {sl:.2f} TP {tp:.2f}  [{tag}]")

    # ------------------------------------------------------------- simulation
    def on_tick(self, ts: int, bid: float, ask: float):
        self.bid, self.ask = bid, ask
        for pos in list(self.positions):
            if pos.direction > 0:
                if bid <= pos.sl:
                    self._close(pos, ts, min(pos.sl, bid), "sl")
                elif bid >= pos.tp:
                    self._close(pos, ts, pos.tp, "tp")
            else:
                if ask >= pos.sl:
                    self._close(pos, ts, max(pos.sl, ask), "sl")
                elif ask <= pos.tp:
                    self._close(pos, ts, pos.tp, "tp")

    def close_all(self, ts):
        for pos in list(self.positions):
            px = self.bid if pos.direction > 0 else self.ask
            self._close(pos, ts, px, "manual")

    def _close(self, pos, ts, px, reason):
        self.positions.remove(pos)
        pnl_gross = (px - pos.entry) * pos.direction
        pnl_pts = pnl_gross - self.cost_pts
        rec = dict(ts_open=pos.ts_open, ts_close=ts, dir=pos.direction,
                   qty=pos.qty, entry=pos.entry, exit=px, sl=pos.sl, tp=pos.tp,
                   reason=reason, pnl_pts=round(pnl_pts, 4),
                   pnl_gross_pts=round(pnl_gross, 4),
                   pnl_usd=round(pnl_pts * self.point_value * pos.qty, 2),
                   tag=pos.tag)
        self.closed.append(rec)
        if self.path:
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow(list(rec.values()))
        self.log(f"CLOSE {reason.upper()} @ {px:.2f} pnl {pnl_pts:+.2f} pts  [{pos.tag}]")

    # ------------------------------------------------------------- reporting
    def summary(self) -> dict:
        n = len(self.closed)
        if n == 0:
            return {"trades": 0}
        pts = [r["pnl_pts"] for r in self.closed]
        wins = sum(1 for p in pts if p > 0)
        gp = sum(p for p in pts if p > 0)
        gl = -sum(p for p in pts if p < 0)
        eq, peak, dd = 0.0, 0.0, 0.0
        for p in pts:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        return {"trades": n, "net_pts": round(sum(pts), 2),
                "net_usd": round(sum(pts) * self.point_value, 2),
                "win_rate": round(100 * wins / n, 1),
                "profit_factor": round(gp / gl, 2) if gl > 0 else float("inf"),
                "max_dd_pts": round(dd, 2),
                "open_positions": len(self.positions)}
