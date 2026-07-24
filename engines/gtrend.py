"""G-Trend: daily trend-following pullback engine for GC (gold futures).

Built from docs/GTREND_SPEC.md (STRATEGY_COMPLETE.md): buy pullbacks in an
uptrend / sell rallies in a downtrend, only when the trend is decisively
moving; fixed 1.5:1 reward:risk brackets sized off ATR; up to 2 concurrent
half-size positions; 10-session time stop.

RULES (decision at the CME session close, 17:00 ET DST-aware; fill at the
next session's first trade = the reopen):
    ret[D]      = close[D] - close[D-1]
    ATR[D]      = mean(high-low, 20 sessions)
    ret_z[D]    = ret[D] / mean(|ret|, 20)
    trend[D]    = sign(close[D] - MA50[D])
    strength[D] = |MA50[D] - MA50[D-10]| / ATR[D]
    ENTER when  strength >= 0.5  AND  0.5 <= |ret_z| <= 4.0
                AND direction (= -sign(ret_z)) == trend
                AND fewer than max_concurrent positions open
    BRACKET     SL = 1.0 x ATR, TP = 1.5 x ATR from the fill
    TIME STOP   flatten after 10 held sessions
Spec metrics (1-unit risk, net of taker costs): 2024 +$25.9k, 2025 +$73.0k
(in-sample), 2026 Jan-Jul OUT-OF-SAMPLE +$103.8k PF 3.68 in a crash year.

Engine-vs-spec differences (all conservative or negligible, be aware):
  - Exits are monitored TICK BY TICK by the broker (server-side SL/TP on
    MT5, tick sim on paper) instead of on daily high/low - this REMOVES the
    spec's pessimistic "stop fills first if a day spans both" assumption.
  - The time stop executes at the NEXT session's open (first tick after the
    boundary), not at the 17:00 close - you cannot trade a price that has
    already closed. Affects only time-stopped trades (~small overnight gap).
  - Prices are NOT roll-adjusted across contract rolls (the spec stitches):
    ret/MA50 spanning a roll date include the futures basis jump. In the
    backtest each contract segment re-warms from real front-month history,
    and positions are flattened at rolls (spec-compliant).
  - The first session after a live start/restart is built from a partial
    day of ticks; signals fully normalize within ~1 session.
NOTE: after a live RESTART the engine no longer tracks previously open MT5
positions, so the 10-session time stop won't fire for them (their SL/TP
remain server-side). Close aged positions manually after long downtime.

DATA-KNOWABILITY GUARANTEE (do not regress this): CME vendor DAILY bars for
session D are only published ~20:00 ET - two hours AFTER the next session
already opened at 18:00 ET. A backtest that decides on "day D's daily bar"
and fills at "day D+1's open" therefore trades on data that did not exist
yet. This engine NEVER consumes vendor daily bars: it builds session bars
from the tick stream itself, so session D's close (last trade before
17:00 ET) is knowable the instant it prints, and the decision runs at the
18:00-ET reopen on ticks <= 17:00 ET only. Verified by perturbation test:
shifting all future ticks +500pts leaves every past entry byte-identical.
If you ever refactor warmup or bar-building, re-run that test.

This engine ignores the L-Rev CLI flags (--rr, --sl-*, --tf, --flow-*,
--max-spread, --order-age); its parameters live in GTREND_CONFIG below and
were FROZEN on 2024-2025 data - do not retune on data you validate with.
"""
from __future__ import annotations

import collections
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

GTREND_CONFIG = {
    "z_win": 20,                # sessions for ATR and |ret| normalization
    "trend_win": 50,            # sessions for the trend MA
    "slope_lag": 10,            # MA slope lookback (sessions)
    "z_entry": 0.5,             # a real pullback ...
    "z_cap": 4.0,               # ... but not a blow-off
    "trend_strength_min": 0.5,  # regime gate: sit out chop
    "stop_atr": 1.0,            # SL distance in ATRs
    "target_atr": 1.5,          # TP distance in ATRs (R:R 1.5)
    "max_hold": 10,             # time stop (held sessions)
    "max_concurrent": 2,        # simultaneous positions
    "qty": 0.5,                 # per-entry size; 2 x 0.5 = 1 unit of risk
    "allow_long": True,
    "allow_short": True,
    "min_session_ticks": 200,   # drop illiquid/partial sessions from signals
    "min_seed_bars": 40,        # min M15 bars for a seeded session to count
    "engine_name": "G-Trend",
    "tag_prefix": "GT",
}

_ET = ZoneInfo("America/New_York")


def _session_date(ts_ns: int) -> int:
    """CME trade date (yyyymmdd) owning this UTC timestamp. Boundary is
    17:00 ET (DST-aware: 21:00 UTC summer / 22:00 UTC winter); everything
    after 17:00 ET belongs to the NEXT trade date (Sunday evening = Monday)."""
    dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(_ET)
    dt = dt + timedelta(hours=7)
    return dt.year * 10000 + dt.month * 100 + dt.day


def _session_end_ns(ts_ns: int) -> int:
    """UTC ns of the next 17:00-ET boundary strictly after ts_ns."""
    dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(_ET)
    close = dt.replace(hour=17, minute=0, second=0, microsecond=0)
    if dt >= close:
        close += timedelta(days=1)          # wall-clock 17:00 next day (DST-safe)
    return int(close.astimezone(timezone.utc).timestamp() * 1e9)


class GTrendStrategy:
    """Daily pullback-with-trend engine. Session bars are built from ticks;
    one comparison per tick (the boundary is precomputed), so tick replay
    stays fast. Decisions happen on the first tick after a session boundary:
    the just-finished session is closed, signals recomputed, time stops
    executed, and a gated entry is filled AT THAT TICK (= next-session open),
    exactly matching the spec's decide-at-close/fill-at-open timing."""

    WARMUP_DAYS = 170            # calendar days of history for live bootstrap
    WANTS_FULL_HISTORY = True    # backtest warmup: seed across ALL contracts
    WARMUP_TFS = ("M15",)        # only M15 bars are needed for seeding
    CONFIG = GTREND_CONFIG
    CLI_DEFAULTS = dict(GTREND_CONFIG)   # runners carry these into cfg

    def __init__(self, broker, config: dict | None = None, log=print):
        cfg = dict(type(self).CONFIG)
        if config:
            cfg.update(config)   # runner cfg also carries L-Rev keys; ignored
        self.cfg = cfg
        self.broker = broker
        self.log = log
        self.now = 0
        self.bid = self.ask = float("nan")
        # current session bar (built from ticks)
        self._sess_end = None
        self._live_sd = None
        self._o = self._h = self._l = self._c = None
        self._nticks = 0
        # signal history
        self._closes = collections.deque(maxlen=cfg["trend_win"])
        self._ranges = collections.deque(maxlen=cfg["z_win"])
        self._absrets = collections.deque(maxlen=cfg["z_win"])
        self._ma_hist = collections.deque(maxlen=cfg["slope_lag"] + 1)
        self._prev_close = None
        self._sessions = 0           # closed sessions seen (incl. seeded)
        self._last_seed_sd = None
        self._sig = None
        self._open_trades = []       # [{tag, entry_sess}]
        self._pending = None         # armed at close, filled on the same tick
        self._prev_count = 0         # open positions as of the previous tick
        self._decision_count = 0     # ... frozen at the session close (spec
                                     # semantics: a stop hit by the reopen gap
                                     # does NOT free a slot for that decision)

    # ---------------------------------------------------------------- seeding
    def seed_bars(self, tf: str, bars):
        """Warmup: aggregate M15 bars into session days (signals only - no
        entries or time stops are generated from seeded history)."""
        if tf != "M15":
            return
        cur = None
        o = h = l = c = None
        n = 0
        for b in bars:
            sd = _session_date(b.t)
            if sd != cur:
                if cur is not None and n >= self.cfg["min_seed_bars"]:
                    self._append_session(o, h, l, c, live=False)
                    self._last_seed_sd = cur
                cur, o, h, l, c, n = sd, b.o, b.h, b.l, b.c, 1
            else:
                h = max(h, b.h)
                l = min(l, b.l)
                c = b.c
                n += 1
        if cur is not None and n >= self.cfg["min_seed_bars"]:
            self._append_session(o, h, l, c, live=False)
            self._last_seed_sd = cur
        self.log(f"[G-Trend] warmup: {self._sessions} session days seeded")

    # ---------------------------------------------------------------- events
    def on_tick(self, ts: int, price: float, size: float, side: str,
                bid: float, ask: float):
        self.now, self.bid, self.ask = ts, bid, ask
        if self._sess_end is None:
            self._sess_end = _session_end_ns(ts)
            self._live_sd = _session_date(ts)
        if ts >= self._sess_end:                 # first tick of a new session
            self._decision_count = self._prev_count   # slots AT the close
            self._close_session()                # close D: signals+stops+decide
            self._sess_end = _session_end_ns(ts)
            self._live_sd = _session_date(ts)
            if self._pending is not None:        # fill at the D+1 open (= now)
                self._fill_pending(ts, price)
        if self._o is None:
            self._o = self._h = self._l = self._c = price
            self._nticks = 1
        else:
            if price > self._h:
                self._h = price
            if price < self._l:
                self._l = price
            self._c = price
            self._nticks += 1
        self._prev_count = self._open_count()

    # ---------------------------------------------------------------- sessions
    def _close_session(self):
        if self._o is None:
            return
        o, h, l, c, n = self._o, self._h, self._l, self._c, self._nticks
        sd = self._live_sd
        self._o = None
        if sd is not None and sd == self._last_seed_sd:
            return                    # partial repeat of the last seeded day
        if n < self.cfg["min_session_ticks"]:
            self.log(f"[G-Trend] session {sd} dropped ({n} ticks)")
            return
        self._append_session(o, h, l, c, live=True)

    def _append_session(self, o, h, l, c, live: bool):
        cfg = self.cfg
        self._sessions += 1
        ret = None if self._prev_close is None else c - self._prev_close
        self._prev_close = c
        self._closes.append(c)
        self._ranges.append(h - l)
        if ret is not None:
            self._absrets.append(abs(ret))

        atr = (sum(self._ranges) / len(self._ranges)
               if len(self._ranges) == cfg["z_win"] else None)
        mean_abs = (sum(self._absrets) / len(self._absrets)
                    if len(self._absrets) == cfg["z_win"] else None)
        ret_z = (ret / mean_abs
                 if (ret is not None and mean_abs and mean_abs > 0) else None)
        ma = trend = tstr = None
        if len(self._closes) == cfg["trend_win"]:
            ma = sum(self._closes) / cfg["trend_win"]
            self._ma_hist.append(ma)
            trend = 1 if c > ma else (-1 if c < ma else 0)
            if (len(self._ma_hist) == self._ma_hist.maxlen
                    and atr and atr > 0):
                tstr = abs(self._ma_hist[-1] - self._ma_hist[0]) / atr
        self._sig = dict(atr=atr, ret_z=ret_z, trend=trend, tstr=tstr, close=c)

        if live:
            freed = self._time_stops()
            # spec semantics: exits managed on day D (incl. the time stop)
            # free their slot for day D's own entry decision
            self._decision_count = max(0, self._decision_count - freed)
            self._decide()

    # ---------------------------------------------------------------- trading
    def _open_count(self) -> int:
        return self.broker.open_count(self.cfg["tag_prefix"] + "|")

    def _time_stops(self) -> int:
        """Flatten positions held >= max_hold sessions. Returns how many
        were closed (they free concurrency slots for this same decision)."""
        cfg = self.cfg
        keep, closed = [], 0
        for t in self._open_trades:
            if self.broker.open_count(t["tag"]) == 0:
                continue                          # already closed by SL/TP
            held = self._sessions - t["entry_sess"]
            if held >= cfg["max_hold"]:
                self.log(f"[G-Trend] TIME STOP after {held} sessions [{t['tag']}]")
                if self.broker.close_position(self.now, t["tag"]):
                    closed += 1
                else:
                    keep.append(t)                # close failed: retry next day
            else:
                keep.append(t)
        self._open_trades = keep
        return closed

    def _decide(self):
        cfg, s = self.cfg, self._sig
        if None in (s["atr"], s["ret_z"], s["trend"], s["tstr"]):
            return
        if s["atr"] <= 0:
            return
        if s["tstr"] < cfg["trend_strength_min"]:
            return                                # regime gate: chop
        az = abs(s["ret_z"])
        if not (cfg["z_entry"] <= az <= cfg["z_cap"]):
            return                                # not a pullback / blow-off
        direction = 1 if s["ret_z"] < 0 else -1   # fade the daily move ...
        if direction != s["trend"]:
            return                                # ... only WITH the trend
        if direction > 0 and not cfg["allow_long"]:
            return
        if direction < 0 and not cfg["allow_short"]:
            return
        if self._decision_count >= cfg["max_concurrent"]:
            return
        self._pending = dict(direction=direction,
                             stop=cfg["stop_atr"] * s["atr"],
                             tgt=cfg["target_atr"] * s["atr"])
        self.log(f"[{cfg['engine_name']}] armed "
                 f"{'BUY dip' if direction > 0 else 'SELL rally'} for the open "
                 f"(z {s['ret_z']:+.2f}, strength {s['tstr']:.2f}, "
                 f"ATR {s['atr']:.2f})")

    def _fill_pending(self, ts, price):
        cfg = self.cfg
        p, self._pending = self._pending, None
        d = p["direction"]
        sl = price - d * p["stop"]
        tp = price + d * p["tgt"]
        tag = f"{cfg['tag_prefix']}|{'L' if d > 0 else 'S'}|{_session_date(ts)}"
        self.broker.market_order(ts, d, cfg["qty"], sl, tp, tag, ref_px=price)
        self._open_trades.append(dict(tag=tag, entry_sess=self._sessions))

    # ---------------------------------------------------------------- misc
    def status(self) -> str:
        s = self._sig
        if not s or s["tstr"] is None:
            return f"G-Trend warming up ({self._sessions} sessions)"
        t = "UP" if s["trend"] > 0 else ("DOWN" if s["trend"] < 0 else "FLAT")
        z = f"{s['ret_z']:+.2f}" if s["ret_z"] is not None else "n/a"
        return (f"trend {t} strength {s['tstr']:.2f} | z {z} | "
                f"{len(self._open_trades)}/{self.cfg['max_concurrent']} positions")

    @staticmethod
    def describe(cfg) -> str:
        return (f"{cfg.get('engine_name', 'G-Trend')} | daily pullback-with-"
                f"trend | SL {cfg['stop_atr']}xATR TP {cfg['target_atr']}xATR "
                f"(R:R {cfg['target_atr'] / cfg['stop_atr']:.1f}) | "
                f"z [{cfg['z_entry']},{cfg['z_cap']}] | "
                f"strength>={cfg['trend_strength_min']} | "
                f"max {cfg['max_concurrent']} x {cfg['qty']} | "
                f"hold<={cfg['max_hold']} sessions")

    def save_state(self, path):
        st = dict(engine=self.cfg["engine_name"], sessions=self._sessions,
                  closes=list(self._closes), ranges=list(self._ranges),
                  absrets=list(self._absrets), ma_hist=list(self._ma_hist),
                  prev_close=self._prev_close, open_trades=self._open_trades,
                  sig=self._sig)
        with open(path, "w") as f:
            json.dump(st, f, indent=1, default=str)


class GTrendLowDD(GTrendStrategy):
    """LOW-DD configuration: tighter entry (z>=0.6), 3 concurrent thirds.
    Lower drawdown / higher win rate; lower net than PRIMARY."""

    CONFIG = dict(GTREND_CONFIG, z_entry=0.6, max_concurrent=3,
                  qty=round(1 / 3, 4), engine_name="G-Trend-LowDD",
                  tag_prefix="GTL")
    CLI_DEFAULTS = dict(CONFIG)
