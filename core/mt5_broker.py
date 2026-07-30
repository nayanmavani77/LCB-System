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

Execution-side safety (the engine's own gates run on the SIGNAL instrument,
so they cannot see any of this):
    pre-flight   algo trading enabled, account allows trading, symbol
                 tradable, --lots x engine qty is a legal volume, hedging
                 vs netting, real-money banner, pre-existing magic positions
    per order    signal-age cap (stale stream), spread cap on the TRADED
                 symbol, exposure cap (never stack positions the engine has
                 forgotten about, e.g. after a restart), broker minimum stop
                 distance, volume snapped to volume_step
    post fill    the position is READ BACK and SL/TP verified against the
                 actual fill; if the broker dropped them they are set, and a
                 position that cannot be protected is closed

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


def snap_volume(lots: float, qty: float, vmin: float, vmax: float,
                vstep: float):
    """Broker-legal order volume for an engine that asked for `qty` units.

    The engine's qty is a SIZE MULTIPLIER (lrev/retf use 1; G-Trend uses
    2 x 0.5 = one unit of risk split in two). --lots is the size of ONE
    unit, so the order volume is lots x qty snapped to the broker's
    volume_step. Returns (volume, error) - error is a human-readable string
    when no legal volume exists, in which case volume is None.
    """
    want = float(lots) * float(qty)
    if want <= 0:
        return None, f"computed volume {want} is not positive"
    vol = want
    if vstep and vstep > 0:
        vol = round(round(want / vstep) * vstep, 8)
    if vmin and vol < vmin - 1e-12:
        need = vmin / float(qty) if qty else vmin
        return None, (f"volume {want:g} (= --lots {lots:g} x engine qty "
                      f"{qty:g}) is below this broker's minimum {vmin:g}; "
                      f"use --lots {need:.4g} or more")
    if vmax and vol > vmax + 1e-12:
        return None, (f"volume {vol:g} exceeds this broker's maximum "
                      f"{vmax:g} for this symbol")
    return vol, None


class MT5Broker(Broker):
    def __init__(self, symbol="XAUUSD", lots=0.01, deviation_points=30,
                 log=print, signal_log_path=None, signal_symbol="GC",
                 max_spread=None, max_positions=None, min_qty=1.0,
                 max_signal_age_s=120.0, max_slip_price=0.30):
        import MetaTrader5 as mt5  # noqa: N813 (Windows-only package)
        self.mt5 = mt5
        self.symbol = symbol
        self.signal_symbol = signal_symbol
        # execution-side guards (the engine's own gates run on the SIGNAL
        # instrument - these run on what is actually being traded)
        self.max_spread = max_spread          # None/0 = no MT5 spread gate
        self.max_positions = max_positions    # None = no broker-side cap
        self.max_signal_age_s = max_signal_age_s
        self.max_slip_price = max_slip_price
        if signal_log_path is None:
            from .paths import log_path
            signal_log_path = log_path(f"mt5_signals_{signal_symbol}.csv")
        self.lots = lots
        self.deviation = deviation_points
        self._oc_cache = {}
        # tag -> position ticket, filled on our own orders. Primary lookup
        # for open_count/close_position; the comment match is only the
        # fallback (brokers may rewrite/truncate deal comments, and the map
        # is empty for positions opened before a restart).
        self._tickets: dict[str, int] = {}
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
                    "status",               # FILLED / FAILED:<retcode> / gate
                    "signal_lag_s",         # stream lag: send time - tick time
                    "mt5_spread",           # spread on the TRADED instrument
                ])
        elif signal_log_path:
            try:                            # header grew: warn, don't corrupt
                with open(signal_log_path, newline="") as f:
                    n = len(next(csv.reader(f)))
                if n != 20:
                    print(f"note: {signal_log_path} has {n} columns (this "
                          f"version writes 20: signal_lag_s and mt5_spread "
                          f"were added). Archive the old file to get a clean "
                          f"header.")
            except (StopIteration, OSError):
                pass

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
            info = mt5.symbol_info(symbol) or info
        acct = mt5.account_info()
        if acct is None:
            raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")
        self.log(f"MT5 connected: account {acct.login} ({acct.server}), "
                 f"executing {symbol} @ {lots} lots per signal")
        self._preflight(mt5, info, acct, lots, min_qty)

    # ----------------------------------------------------------- pre-flight
    def _preflight(self, mt5, info, acct, lots, min_qty):
        """Fail FAST on everything that would otherwise only surface as a
        rejected order in the middle of the trading window - or, worse, as a
        silently mis-sized position. Called once at connect time."""
        self.digits = int(getattr(info, "digits", 2))
        self.point = float(getattr(info, "point", 0.0) or 10 ** -self.digits)
        self.vmin = float(getattr(info, "volume_min", 0.0) or 0.0)
        self.vmax = float(getattr(info, "volume_max", 0.0) or 0.0)
        self.vstep = float(getattr(info, "volume_step", 0.0) or 0.0)
        self.stops_level = int(getattr(info, "trade_stops_level", 0) or 0)
        self.contract = float(getattr(info, "trade_contract_size", 0.0) or 0.0)

        # slippage cap in PRICE, converted to points: a hard-coded 30 points
        # means $0.30 on a 2-digit gold feed but only $0.03 on a 3-digit one,
        # which would get every order requoted in a fast market
        self.deviation = max(int(self.deviation),
                             max(1, int(round(self.max_slip_price / self.point))))

        # 1. algorithmic trading actually permitted?
        term = mt5.terminal_info()
        if term is not None and not getattr(term, "trade_allowed", True):
            raise RuntimeError(
                "MT5 terminal has algorithmic trading DISABLED - every order "
                "would be rejected. Enable Tools > Options > Expert Advisors "
                "> 'Allow algorithmic trading', then restart this runner.")
        if not getattr(acct, "trade_allowed", True):
            raise RuntimeError(
                f"account {acct.login} does not allow trading (investor / "
                f"read-only password, or trading disabled by the broker)")
        if not getattr(acct, "trade_expert", True):
            raise RuntimeError(
                f"account {acct.login} has EXPERT (automated) trading "
                f"disabled server-side - contact the broker")

        # 2. is this symbol tradable right now, or quote-only?
        full = getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)
        tmode = getattr(info, "trade_mode", full)
        if tmode != full:
            self.log(f"WARNING: {self.symbol} trade_mode={tmode} is not full "
                     f"trading (close-only / quotes-only / disabled) - orders "
                     f"may be rejected until the session opens")

        # 3. will the engine's smallest order be a legal volume?
        vol, err = snap_volume(lots, min_qty, self.vmin, self.vmax, self.vstep)
        if err:
            raise RuntimeError(f"{self.symbol}: {err}")
        if abs(vol - lots * min_qty) > 1e-9:
            self.log(f"note: order volume snapped to the broker's step "
                     f"{self.vstep:g}: {lots * min_qty:g} -> {vol:g}")

        # 4. netting accounts merge same-symbol positions: per-position SL/TP
        #    and any multi-position engine stop behaving like the backtest
        hedging = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        mmode = getattr(acct, "margin_mode", hedging)
        if mmode != hedging:
            self.log("WARNING: this account is NETTING, not hedging. "
                     "Same-symbol positions merge into one net position, so "
                     "per-position SL/TP and multi-position engines will NOT "
                     "reproduce the backtest. Use a hedging account.")

        # 5. real money? say so, loudly, every single session
        if getattr(acct, "trade_mode", 0) != getattr(
                mt5, "ACCOUNT_TRADE_MODE_DEMO", 0):
            self.log(f"*** REAL-MONEY ACCOUNT {acct.login} - orders will use "
                     f"actual funds (balance {acct.balance:,.2f} "
                     f"{acct.currency}) ***")

        # 6. positions already carrying our magic: engine state knows nothing
        #    about them, so say so and let the broker-side cap protect us
        pre = [p for p in (mt5.positions_get(symbol=self.symbol) or [])
               if p.magic == MAGIC]
        if pre:
            self.log(f"WARNING: {len(pre)} pre-existing position(s) on "
                     f"{self.symbol} carry our magic {MAGIC}. The engine "
                     f"starts flat and does not manage them; they keep their "
                     f"server-side SL/TP.")
            for p in pre:
                self.log(f"   ticket {p.ticket} vol {p.volume} "
                         f"open {p.price_open} sl {p.sl} tp {p.tp} "
                         f"comment '{p.comment}'")

        cap = ("off" if self.max_positions is None
               else f"{self.max_positions} position(s)")
        self.log(f"MT5 pre-flight OK: digits {self.digits}, point "
                 f"{self.point:g}, volume {self.vmin:g}/{self.vstep:g}/"
                 f"{self.vmax:g}, stops level {self.stops_level} pts, "
                 f"slippage cap {self.deviation} pts "
                 f"(${self.deviation * self.point:.2f}), spread gate "
                 f"{self.max_spread if self.max_spread else 'off'}, "
                 f"exposure cap {cap}, signal-age cap "
                 f"{self.max_signal_age_s or 'off'}s")

    def _log_row(self, ts, direction, tag, ref_px, sl, tp, sl_dist, tp_dist,
                 bid="", ask="", fill="", sl_x="", tp_x="", status="",
                 vol="", lag="", spread=""):
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
                self.symbol, vol if vol != "" else self.lots,
                bid, ask, fill, sl_x, tp_x, status, lag, spread])

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
        spread = tick.ask - tick.bid
        lag = max(0.0, time.time() - ts / 1e9) if ts else 0.0

        def drop(status, why):
            self.log(f"MT5 signal DROPPED ({why}) [{tag}]")
            self._log_row(ts, direction, tag, ref_px, sl, tp, sl_dist, tp_dist,
                          round(tick.bid, 2), round(tick.ask, 2),
                          status=status, lag=round(lag, 1),
                          spread=round(spread, 3))

        # ---- execution-side gates. The engine's gates ran on the SIGNAL
        # instrument (GC); these run on what is actually being traded and on
        # how old the signal is by the time it reaches the broker.
        if self.max_signal_age_s and lag > self.max_signal_age_s:
            return drop(f"STALE:{lag:.0f}s",
                        f"signal is {lag:.0f}s old (> {self.max_signal_age_s}s "
                        f"cap) - the tick stream is lagging, the price this "
                        f"decision was made on no longer exists")
        if self.max_spread and spread > self.max_spread:
            return drop("SPREAD", f"{self.symbol} spread {spread:.2f} > cap "
                                  f"{self.max_spread} (the engine's gate only "
                                  f"saw the {self.signal_symbol} spread)")
        if self.max_positions is not None:
            n_open = len([p for p in (mt5.positions_get(symbol=self.symbol)
                                      or []) if p.magic == MAGIC])
            if n_open >= self.max_positions:
                return drop("MAX_POSITIONS",
                            f"{n_open} position(s) with our magic already "
                            f"open on {self.symbol}, cap is "
                            f"{self.max_positions} - refusing to stack "
                            f"exposure the engine does not know about")
        if sl_dist <= 0 or tp_dist <= 0:
            return drop("BAD_GEOMETRY", f"SL distance {sl_dist:.2f} / TP "
                                        f"distance {tp_dist:.2f} is not "
                                        f"positive")
        min_dist = self.stops_level * self.point
        if min_dist and min(sl_dist, tp_dist) < min_dist:
            return drop("STOPS_LEVEL",
                        f"SL/TP distance {min(sl_dist, tp_dist):.2f} is inside "
                        f"the broker's minimum stop distance {min_dist:.2f} - "
                        f"the order would be rejected or placed unprotected")
        volume, verr = snap_volume(self.lots, qty, self.vmin, self.vmax,
                                   self.vstep)
        if verr:
            return drop("BAD_VOLUME", verr)

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
            "volume": volume,      # --lots x the engine's qty, step-snapped
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
        if result is None:      # terminal dropped mid-retry - do not crash
            self.log(f"MT5 order_send returned None on retry: "
                     f"{mt5.last_error()} [{tag}] - VERIFY POSITION MANUALLY")
            self._log_row(ts, direction, tag, ref_px, sl, tp, sl_dist, tp_dist,
                          round(tick.bid, 2), round(tick.ask, 2),
                          sl_x=sl_x, tp_x=tp_x, status="SEND_ERROR_RETRY")
            return
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        status = "FILLED" if ok else f"FAILED:{result.retcode}"
        if ok:
            ticket = self._resolve_position(result)
            self._tickets[tag] = ticket           # close/count lookup key
            self._oc_cache.clear()                # cache is stale immediately
            self.log(f"MT5 {'BUY' if direction > 0 else 'SELL'} {volume:g} "
                     f"{self.symbol} @ {result.price:.2f} SL {sl_x} TP {tp_x} "
                     f"[{tag}] (position {ticket}, signal lag {lag:.1f}s, "
                     f"spread {spread:.2f}, slip "
                     f"{(result.price - px) * direction:+.2f})")
            # VERIFY, then re-anchor. Two separate failures are covered here:
            #  a) some brokers accept the DEAL but silently DROP the attached
            #     SL/TP -> the position would sit completely unprotected;
            #  b) the fill slipped from the quoted price -> the distances must
            #     be re-measured from the ACTUAL fill or the risk geometry no
            #     longer matches the backtest.
            sl_x, tp_x, status = self._ensure_protection(
                ticket, tag, direction, sl_dist, tp_dist, sl_x, tp_x,
                result.price, digits)
        else:
            self.log(f"MT5 order FAILED retcode={result.retcode} "
                     f"comment={result.comment} [{tag}]")
        self._log_row(ts, direction, tag, ref_px, sl, tp, sl_dist, tp_dist,
                      round(tick.bid, 2), round(tick.ask, 2),
                      round(result.price, 2) if ok else "", sl_x, tp_x,
                      status, vol=volume if ok else "",
                      lag=round(lag, 1), spread=round(spread, 3))

    # -------------------------------------------------- post-fill protection
    def _resolve_position(self, result):
        """Position ticket for a just-filled order. On hedging accounts the
        position ticket equals the order ticket; on netting accounts the
        position carries `identifier` = the originating order. Match on both
        rather than assuming, then fall back to the order ticket."""
        want = result.order
        for _ in range(5):            # the position can lag the deal slightly
            try:
                for p in (self.mt5.positions_get(symbol=self.symbol) or []):
                    if p.magic != MAGIC:
                        continue
                    if p.ticket == want \
                            or getattr(p, "identifier", None) == want:
                        return p.ticket
            except Exception:
                pass
            time.sleep(0.2)
        return want

    def _position(self, ticket):
        try:
            for p in (self.mt5.positions_get(symbol=self.symbol) or []):
                if p.ticket == ticket:
                    return p
        except Exception:
            pass
        return None

    def _ensure_protection(self, ticket, tag, direction, sl_dist, tp_dist,
                           sl_x, tp_x, fill_px, digits):
        """Make sure the position really carries SL and TP, measured from its
        ACTUAL open price. Returns (sl, tp, status)."""
        mt5 = self.mt5
        p = self._position(ticket)
        fill = float(getattr(p, "price_open", fill_px) or fill_px)
        if direction > 0:
            want_sl = round(fill - sl_dist, digits)
            want_tp = round(fill + tp_dist, digits)
        else:
            want_sl = round(fill + sl_dist, digits)
            want_tp = round(fill - tp_dist, digits)
        if p is None:
            self.log(f"CRITICAL: filled but the position is not visible in "
                     f"MT5 yet [{tag}] - VERIFY SL/TP MANUALLY in the terminal")
            return sl_x, tp_x, "FILLED_UNVERIFIED"
        naked = (not p.sl) or (not p.tp)
        drifted = (abs(p.sl - want_sl) >= self.point
                   or abs(p.tp - want_tp) >= self.point)
        if not naked and not drifted:
            return round(p.sl, digits), round(p.tp, digits), "FILLED"
        if naked:
            self.log(f"WARNING: broker did not attach "
                     f"{'SL' if not p.sl else ''}{'/' if not p.sl and not p.tp else ''}"
                     f"{'TP' if not p.tp else ''} to position {ticket} - "
                     f"setting it now [{tag}]")
        for attempt in range(3):
            r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP,
                                "symbol": self.symbol, "position": ticket,
                                "sl": want_sl, "tp": want_tp, "magic": MAGIC})
            if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
                self.log(f"MT5 SL/TP set from actual fill {fill:.2f}: "
                         f"SL {sl_x}->{want_sl} TP {tp_x}->{want_tp} [{tag}]")
                return want_sl, want_tp, "FILLED"
            rc = r.retcode if r is not None else "send_error"
            self.log(f"MT5 SL/TP modify rejected (retcode={rc}, attempt "
                     f"{attempt + 1}/3) [{tag}]")
            time.sleep(1.0)
        p = self._position(ticket) or p
        if p.sl and p.tp:
            self.log(f"note: keeping the broker's own SL {p.sl} / TP {p.tp} "
                     f"on position {ticket} - geometry differs slightly from "
                     f"the signal but the position IS protected [{tag}]")
            return round(p.sl, digits), round(p.tp, digits), "FILLED_DRIFTED"
        self.log(f"CRITICAL: position {ticket} is UNPROTECTED (sl={p.sl} "
                 f"tp={p.tp}) and SL/TP could not be set. Closing it "
                 f"immediately - an unstopped position is a bigger risk than "
                 f"a missed trade. [{tag}]")
        if self.close_position(0, tag):
            return p.sl, p.tp, "CLOSED_UNPROTECTED"
        self.log(f"CRITICAL: could not close the unprotected position "
                 f"{ticket} either - CLOSE IT MANUALLY IN MT5 NOW [{tag}]")
        return p.sl, p.tp, "OPEN_UNPROTECTED"

    # ------------------------------------------------------------- queries
    def _my_positions(self, tag_prefix: str = ""):
        """Our open positions matching a tag prefix: by TICKET first (from
        orders we sent this session), by comment as fallback."""
        pos = self.mt5.positions_get(symbol=self.symbol) or []
        mine = [p for p in pos if p.magic == MAGIC]
        live_tickets = {p.ticket for p in mine}
        live_tickets |= {getattr(p, "identifier", None) for p in mine}
        # prune tickets whose positions are gone (closed by SL/TP)
        self._tickets = {t: k for t, k in self._tickets.items()
                         if k in live_tickets}
        wanted = {k for t, k in self._tickets.items()
                  if t.startswith(tag_prefix)}
        return [p for p in mine
                if p.ticket in wanted
                or getattr(p, "identifier", None) in wanted
                or p.comment.startswith(tag_prefix)]

    def open_count(self, tag_prefix: str = "") -> int:
        # 1s TTL cache: engines may query this per tick; a terminal RPC per
        # tick is wasteful and 1s staleness is irrelevant at daily cadence
        import time as _t
        now = _t.monotonic()
        cached = self._oc_cache.get(tag_prefix)
        if cached and now - cached[0] < 1.0:
            return cached[1]
        n = len(self._my_positions(tag_prefix))
        self._oc_cache[tag_prefix] = (now, n)
        return n

    def close_position(self, ts, tag: str) -> bool:
        """Close our open position for this tag (used by engines for time
        stops): ticket lookup first, comment match as fallback. Sends an
        opposite DEAL against the ticket."""
        mt5 = self.mt5
        want_ticket = self._tickets.get(tag)
        for p in self._my_positions():
            if p.ticket != want_ticket and p.comment != tag[:31]:
                continue
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                self.log(f"MT5 close: no tick, retry next session [{tag}]")
                return False
            closing_buy = p.type == mt5.POSITION_TYPE_SELL
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "position": p.ticket,
                "volume": p.volume,
                "type": (mt5.ORDER_TYPE_BUY if closing_buy
                         else mt5.ORDER_TYPE_SELL),
                "price": tick.ask if closing_buy else tick.bid,
                "deviation": self.deviation,
                "magic": MAGIC,
                "comment": ("close:" + tag)[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result is not None and result.retcode != mt5.TRADE_RETCODE_DONE:
                request["type_filling"] = mt5.ORDER_FILLING_FOK
                result = mt5.order_send(request)
            ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
            if ok:
                self._tickets.pop(tag, None)
                self._oc_cache.clear()           # count changed right now
                self.log(f"MT5 CLOSED position {p.ticket} @ "
                         f"{result.price:.2f} [{tag}]")
            else:
                rc = result.retcode if result is not None else "send_error"
                self.log(f"MT5 close FAILED retcode={rc} [{tag}] - "
                         f"position keeps its server-side SL/TP")
            return ok
        self.log(f"MT5 close: no open position matched [{tag}] (ticket "
                 f"{want_ticket}); it was probably already closed by its "
                 f"SL/TP - nothing to do")
        return False

    def move_sl_to_breakeven(self, ts, tag: str) -> bool:
        """Move the position's SL to its actual open price (server-side)."""
        mt5 = self.mt5
        want_ticket = self._tickets.get(tag)
        for p in self._my_positions():
            if p.ticket != want_ticket and p.comment != tag[:31]:
                continue
            req = {"action": mt5.TRADE_ACTION_SLTP,
                   "symbol": self.symbol,
                   "position": p.ticket,
                   "sl": p.price_open,
                   "tp": p.tp,
                   "magic": MAGIC}
            result = mt5.order_send(req)
            ok = (result is not None
                  and result.retcode == mt5.TRADE_RETCODE_DONE)
            if ok:
                self.log(f"MT5 BREAKEVEN SL -> {p.price_open:.2f} "
                         f"(position {p.ticket}) [{tag}]")
            else:
                rc = result.retcode if result is not None else "send_error"
                self.log(f"MT5 breakeven modify FAILED retcode={rc} [{tag}] "
                         f"- original SL kept (position stays protected)")
            return ok
        self.log(f"MT5 breakeven: no open position matched [{tag}] (ticket "
                 f"{want_ticket}) - skipping")
        return False

    def shutdown(self):
        self.mt5.shutdown()
