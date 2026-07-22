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

from .broker import Broker

MAGIC = 26031604  # matches the old EA's L-System magic range


class MT5Broker(Broker):
    def __init__(self, symbol="XAUUSD", lots=0.01, deviation_points=30,
                 log=print):
        import MetaTrader5 as mt5  # noqa: N813 (Windows-only package)
        self.mt5 = mt5
        self.symbol = symbol
        self.lots = lots
        self.deviation = deviation_points
        self.log = log

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

    def market_order(self, ts, direction, qty, sl, tp, tag, ref_px=None):
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            self.log(f"MT5: no tick for {self.symbol}, signal dropped [{tag}]")
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
            return
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            # retry once with FOK filling (broker-dependent)
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log(f"MT5 {'BUY' if direction > 0 else 'SELL'} {self.lots} "
                     f"{self.symbol} @ {result.price:.2f} SL {sl_x} TP {tp_x} "
                     f"[{tag}]")
        else:
            self.log(f"MT5 order FAILED retcode={result.retcode} "
                     f"comment={result.comment} [{tag}]")

    def shutdown(self):
        self.mt5.shutdown()
