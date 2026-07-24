"""MT5 broker adapter: execute L-Rev signals (generated from GC data) on
XAUUSD (or any gold symbol) in MetaTrader 5.

How the GC -> XAUUSD translation works:
    GC futures and spot gold are both $/oz; their MOVES are ~identical and
    only the price LEVEL differs by the futures basis. So absolute GC prices
    are never sent to MT5. Instead the SL/TP DISTANCES from the signal's
    trigger price are applied to XAUUSD's own live price:
        buy  : sl = xau_fill - (ref_px - sl_gc),  tp = xau_fill + (tp_gc - ref_px)
    The basis cancels out. Direction, timing and risk geometry carry over 1:1.
    1.00 lot XAUUSD = 100 oz = same $/point as 1 GC contract; qty here is LOTS.

Requirements (Windows only):
    pip install MetaTrader5
    - MT5 terminal installed, running and logged into your account
    - Tools > Options > Expert Advisors > "Allow algorithmic trading"

Usage:
    python3 live.py --broker mt5 --mt5-symbol XAUUSD --lots 0.01

ALWAYS test on a DEMO account first and compare its trades against
paper_trades.csv for a couple of weeks before any real account.
"""
from __future__ import annotations

import csv
import os
import time

from .broker import Broker

MAGIC = 26031604  # matches the old EA's L-System magic range


class MT5Broker(Broker):
    def __init__(self, symbol="XAUUSD", lots=0.01, deviation_points=30,
                 log=print, signal_log_path=None, signal_symbol="GC"):
        import MetaTrader5 as mt5  # noqa: N813 (Windows-only package)
        self.mt5 = mt5
        self.symbol = symbol
        self.signal_symbol = signal_symbol
        if signal_log_path is None:
            from .paths import log_path
            signal_log_path = log_path(f"mt5_signals_{signal_symbol}.csv")
        self.lots = lots
        self.deviation = deviation_points
        self.log = log
        self.signal_log_path = signal_log_path
        if signal_log_path and not os.path.exists(signal_log_path):
            with open(signal_log_path, "w", newline="") as f:
                csv.writer(f).writerow([
                    # ---- signal side: what the strategy saw and decided ----
                    "sig_symbol",           # futures symbol the signal came from
                    "sig_time_utc",         # trigger tick time (exchange)
                    "direction",            # BUY / SELL
                    "signal_source",        # timeframe + level, e.g. L-Rev|M15|high@4154.60
                    "sig_trigger_px",       # futures price that fired the signal
                    "sig_sl", "sig_tp",     # SL/TP in futures prices
                    "sl_dist", "tp_dist",   # distances carried to MT5
                    # ---- MT5 side: what was actually executed ----
                    "mt5_order_time_utc",   # when the order was sent
                    "mt5_symbol", "lots",
                    "mt5_bid", "mt5_ask",   # XAUUSD quote at order time
                    "mt5_fill_px",          # actual fill
                    "mt5_sl", "mt5_tp",     # SL/TP as placed on MT5
                    "status",               # FILLED / FAILED:<retcode>
                ])

        # Optional explicit login from config.py (MT5_LOGIN/PASSWORD/SERVER).
        # If absent, attach to whatever account the running terminal has open.
        login = password = server = None
        try:
            import config
            login = getattr(config, "MT5_LOGIN", None)
            password = getattr(config, "MT5_PASSWORD", None)
            server = getattr(config, "MT5_SERVER", None)
        except ImportError:
            pass
        if login:
            ok = mt5.initialize(login=int(login), password=password,
                                server=server)
        else:
            ok = mt5.initialize()
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol {symbol} not found in MT5")
        if not info.visible:
            mt5.symbol_select(symbol, True)
        acct = mt5.account_info()
        self.log(f"MT5 connected: account {acct.login} ({acct.server}), "
                 f"executing {symbol} @ {lots} lots per signal")

    def _log_row(self, ts, direction, tag, ref_px, sl, tp, sl_dist, tp_dist,
                 bid="", ask="", fill="", sl_x="", tp_x="", status=""):
        if not self.signal_log_path:
            return
        gc_time = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.gmtime(ts / 1e9)) if ts else ""
        with open(self.signal_log_path, "a", newline="") as f:
            csv.writer(f).writerow([
                self.signal_symbol,
                gc_time, "BUY" if direction > 0 else "SELL", tag,
                round(ref_px, 2), round(sl, 2), round(tp, 2),
                round(sl_dist, 2), round(tp_dist, 2),
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                self.symbol, self.lots, bid, ask, fill, sl_x, tp_x, status])

    def market_order(self, ts, direction, qty, sl, tp, tag, ref_px=None):
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            self.log(f"MT5: no tick for {self.symbol}, signal dropped [{tag}]")
            _ref = ref_px if ref_px is not None else (sl + tp) / 2.0
            self._log_row(ts, direction, tag, _ref, sl, tp,
                          abs(_ref - sl), abs(tp - _ref), status="NO_TICK")
            return
        info = mt5.symbol_info(self.symbol)
        digits = info.digits

        # translate GC-absolute SL/TP into distances, re-anchor on XAUUSD
        if ref_px is None:
            ref_px = (sl + tp) / 2.0  # should not happen; defensive
        sl_dist = abs(ref_px - sl)
        tp_dist = abs(tp - ref_px)

        if direction > 0:
            px = tick.ask
            sl_x = round(px - sl_dist, digits)
            tp_x = round(px + tp_dist, digits)
            otype = mt5.ORDER_TYPE_BUY
        else:
            px = tick.bid
            sl_x = round(px + sl_dist, digits)
            tp_x = round(px - tp_dist, digits)
            otype = mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self.lots,
            "type": otype,
            "price": px,
            "sl": sl_x,
            "tp": tp_x,
            "deviation": self.deviation,
            "magic": MAGIC,
            "comment": tag[:31],          # MT5 comment length limit
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            self.log(f"MT5 order_send returned None: {mt5.last_error()} [{tag}]")
            self._log_row(ts, direction, tag, ref_px, sl, tp, sl_dist, tp_dist,
                          round(tick.bid, 2), round(tick.ask, 2),
                          sl_x=sl_x, tp_x=tp_x, status="SEND_ERROR")
            return
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            # retry once with FOK filling (broker-dependent)
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        if ok:
            self.log(f"MT5 {'BUY' if direction > 0 else 'SELL'} {self.lots} "
                     f"{self.symbol} @ {result.price:.2f} SL {sl_x} TP {tp_x} "
                     f"[{tag}]")
            # Precision re-anchor: if the fill slipped from the quoted price,
            # move SL/TP so distances are exact from the ACTUAL fill. The
            # position is protected by the original SL/TP the whole time.
            if abs(result.price - px) >= 0.01:
                fill = result.price
                if direction > 0:
                    new_sl = round(fill - sl_dist, digits)
                    new_tp = round(fill + tp_dist, digits)
                else:
                    new_sl = round(fill + sl_dist, digits)
                    new_tp = round(fill - tp_dist, digits)
                mod = {"action": mt5.TRADE_ACTION_SLTP,
                       "symbol": self.symbol,
                       "position": result.order,
                       "sl": new_sl, "tp": new_tp, "magic": MAGIC}
                r2 = mt5.order_send(mod)
                if r2 is not None and r2.retcode == mt5.TRADE_RETCODE_DONE:
                    self.log(f"MT5 re-anchored SL {sl_x}->{new_sl} "
                             f"TP {tp_x}->{new_tp} on fill {fill:.2f}")
                    sl_x, tp_x = new_sl, new_tp
                else:
                    self.log("MT5 re-anchor modify rejected; keeping "
                             "original SL/TP (position stays protected)")
        else:
            self.log(f"MT5 order FAILED retcode={result.retcode} "
                     f"comment={result.comment} [{tag}]")
        self._log_row(ts, direction, tag, ref_px, sl, tp, sl_dist, tp_dist,
                      round(tick.bid, 2), round(tick.ask, 2),
                      round(result.price, 2) if ok else "", sl_x, tp_x,
                      "FILLED" if ok else f"FAILED:{result.retcode}")

    def shutdown(self):
        self.mt5.shutdown()
