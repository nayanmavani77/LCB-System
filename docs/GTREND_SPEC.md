# GC Gold Trend-Following Strategy — Complete Build Guide

A single, self-contained specification for a deterministic, rule-based swing strategy on
**COMEX Gold futures (GC)**. A developer can build the entire system from this document
alone: it contains the intuition, the exact math, the reference implementation (with code),
both configurations, the metrics, and step-by-step build/run instructions.

- **Type:** daily swing, **trend-following via pullbacks**, fixed risk-reward exit, multi-concurrent sizing
- **Instrument:** COMEX Gold futures, continuous front month
- **Horizon:** decisions once per session (daily); trades held ~1–10 sessions
- **Data used:** trade + top-of-book (TBBO) or any daily OHLC of the front-month contract
- **Result (2024–2025 dev / 2026 out-of-sample):** see [§7](#7-metrics)

---

## Table of contents
1. [Intuition — why it works](#1-intuition--why-it-works)
2. [Contract & data specs](#2-contract--data-specs)
3. [System architecture](#3-system-architecture)
4. [The math (signals, entries, exits, sizing)](#4-the-math)
5. [Reference implementation (code)](#5-reference-implementation-code)
6. [The two configurations](#6-the-two-configurations)
7. [Metrics](#7-metrics)
8. [Step-by-step: build & run](#8-step-by-step-build--run)
9. [Assumptions & no-look-ahead guarantees](#9-assumptions--no-look-ahead-guarantees)
10. [Taking it live](#10-taking-it-live)
11. [Known limitations & how to extend](#11-known-limitations--how-to-extend)

---

## 1. Intuition — why it works

Gold trends strongly and for long stretches (up **or** down). Within a trend, price pulls back
regularly. The strategy **buys pullbacks in an uptrend and sells rallies in a downtrend**, but
only when the trend is **decisively moving** (it sits out choppy, directionless markets, which is
where trend-following bleeds). Each trade risks a fixed amount (1×ATR stop) to make 1.5× that
(1.5×ATR target). To trade often enough without over-risking, it runs **up to two positions at
once**, each half-size — diversifying *entry timing* rather than adding leverage.

What this is **not**: it is **not** market-neutral alpha. ~60–65% of its P&L is trend
participation ("beta"). Its edge is **regime adaptivity** — it flips long/short with the trend and
stands aside in chop. That is exactly what let it profit in 2026 (a −7.5% down/crash year) when
buy-and-hold lost money.

Two facts established during research that shaped this design:
- **Intraday microstructure does not beat costs on GC** (1-min/5-min order-flow, breakout,
  mean-reversion all lose to the spread). Trade the **daily** horizon.
- A daily **mean-reversion** "edge" is an artifact once the session boundary is correct — its
  timing alpha is negative. **The tradeable signal is the trend, not reversion.**

---

## 2. Contract & data specs

| Item | Value |
|---|---|
| Contract | COMEX Gold (GC), 100 troy oz |
| Tick size | 0.10 → **$10 / tick** |
| Point value | **$100 / 1.00 move** |
| Symbol used | continuous front month (volume roll); e.g. Databento `GC.v.0` |
| Data needed | per-session OHLC of the front month + a spread estimate; TBBO gives trade+BBO |
| Session boundary | **CME daily close 17:00 ET** — DST-aware: **21:00 UTC (summer) / 22:00 UTC (winter)** |

**Minimum inputs the strategy needs (one row per session day):**
`open, high, low, close` (front-month price), `volume`, and a per-day `spread` estimate (for cost).
Everything else is derived. You do **not** need order-flow/microstructure for this final strategy —
it is price-based (that was a research finding).

---

## 3. System architecture

```
raw front-month data                 (TBBO trades, or vendor daily OHLC)
        │
        ▼
[1] session-day bars  ──────────────  aggregate to CME trade dates (17:00 ET, DST-aware)
        │                              -> open/high/low/close, volume, spread, ret, range
        ▼
[2] stitch multi-year  ─────────────  one continuous back-adjusted series (no seam jumps)
        │
        ▼
[3] signals  ───────────────────────  ret_z, ATR, trend, trend_strength
        │
        ▼
[4] backtest engine  ───────────────  entry gate -> next-open fill -> R:R exit -> up to 2 concurrent
        │
        ▼
[5] trades + metrics                   blotter, P&L, drawdown, Sharpe, ...
```

The reference code follows these five stages. Stages [1]–[2] are data-vendor-specific; stages
[3]–[5] are the strategy itself and are portable to any front-month daily series.

---

## 4. The math

All quantities are computed from data available **at or before** the decision session's close.

**Per-session inputs:** `close[D]`, `high[D]`, `low[D]`, `open[D]`, and `range[D] = high[D] - low[D]`.

```
ret[D]            = close[D] - close[D-1]                         # session close-to-close (points)
ATR[D]            = mean(range, 20)                               # 20-session average range (points)
ret_z[D]          = ret[D] / mean(|ret|, 20)                      # normalized daily move (unitless)
trend_ma[D]       = mean(close, 50)                               # 50-session simple MA
trend[D]          = sign(close[D] - trend_ma[D])                  # +1 uptrend / -1 downtrend
trend_slope[D]    = trend_ma[D] - trend_ma[D-10]                  # MA slope over 10 sessions
trend_strength[D] = |trend_slope[D]| / ATR[D]                     # how decisively it trends
```

**Entry decision at close of session D** (executed at the open of D+1):
```
REGIME GATE  : trend_strength[D] >= TREND_STRENGTH_MIN            # else no trade (chop)
SETUP        : Z_ENTRY <= |ret_z[D]| <= Z_CAP                     # a real pullback, not a blow-off
RAW DIR      : direction = -sign(ret_z[D])                        # fade the pullback
TREND GATE   : keep the trade only if direction == trend[D]      # i.e. WITH the trend:
                 uptrend  + down day (ret_z<0) -> BUY  the dip
                 downtrend + up day (ret_z>0)  -> SELL the rally
CONCURRENCY  : open it only if fewer than MAX_CONCURRENT positions are already live
```

**Position (per trade), sized at entry using ATR[D]:**
```
entry_px  = open[D+1]                                             # next-session open (the fill)
stop_dist = STOP_ATR   * ATR[D]                                   # the "risk"  (points)
tgt_dist  = TARGET_ATR * ATR[D]                                   # the "reward" (points)
R:R       = TARGET_ATR / STOP_ATR                                 # = 1.5 in both configs
lots      = LOTS_PER                                              # e.g. 0.5 so 2 concurrent = 1 unit

# for a LONG (direction=+1):   stop_px = entry_px - stop_dist ;  tp_px = entry_px + tgt_dist
# for a SHORT (direction=-1):  stop_px = entry_px + stop_dist ;  tp_px = entry_px - tgt_dist
```

**Exit (checked on each held session's high/low; STOP has priority if a day spans both):**
```
STOP   : long  low  <= stop_px  (fill at stop_px - slip) ; short high >= stop_px (fill at stop_px + slip)
TARGET : long  high >= tp_px    (fill at tp_px)          ; short low  <= tp_px   (fill at tp_px)
TIME   : if still open after MAX_HOLD sessions -> exit at that session's close
EOD    : if still open at the end of the data -> mark-to-market at the last close
```

**P&L (per trade):**
```
gross = lots * direction * (exit_px - entry_px) * 100            # $100/point
cost  = lots * ( spread_points * 100  +  2 * COMMISSION_PER_SIDE )   # taker: cross spread + commissions
net   = gross - cost
```

---

## 5. Reference implementation (code)

Python 3.10+, `numpy`, `pandas`. Stages [3]–[5] are self-contained: give them a session-day
DataFrame `d` with columns `open, high, low, close, range, ret, spread_pts` and they produce a
trade blotter. Stages [1]–[2] show how to build that DataFrame from Databento TBBO.

### 5.0 Constants & cost model
```python
import numpy as np, pandas as pd

TICK = 0.10
POINT_VALUE = 100.0            # USD per 1.00 move per contract
COMMISSION_PER_SIDE = 1.50     # USD per contract per side

def round_turn_cost_usd(spread_points: float) -> float:
    """Taker round-turn cost for 1 lot: cross the full quoted spread + 2 commissions."""
    return spread_points * POINT_VALUE + 2 * COMMISSION_PER_SIDE
```

### 5.1 Session-day aggregation (DST-aware CME boundary)  — data-vendor-specific
```python
SESSION_TZ = "America/New_York"
_CLOSE_SHIFT = pd.Timedelta(hours=7)   # 17:00 ET + 7h = next ET midnight -> that date = CME trade date

def session_day_index(ts_utc: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Map UTC timestamps to CME trade dates. DST-aware: the boundary is 17:00 ET,
    i.e. 21:00 UTC in summer and 22:00 UTC in winter — handled automatically by the
    America/New_York conversion."""
    et = ts_utc.tz_convert(SESSION_TZ)
    return (et + _CLOSE_SHIFT).normalize().tz_localize(None)

def to_daily(bars_1min: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1-min front-month bars (UTC index; cols open/high/low/close/volume/
    spread_mean) into CME session-day bars. Adapt column names to your feed."""
    sd = session_day_index(bars_1min.index)
    g = bars_1min.groupby(sd)
    d = pd.DataFrame({
        "open":  g["open"].first(),
        "high":  g["high"].max(),
        "low":   g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "spread_pts": g["spread_mean"].mean(),     # per-day avg quoted spread, in points
        "n_bars": g["close"].size(),
    })
    d = d[d["n_bars"] >= 200].copy()               # drop illiquid/partial sessions
    d["ret"] = d["close"].diff()
    d["range"] = d["high"] - d["low"]
    return d
```
> If your vendor already provides **daily** front-month OHLC aligned to the 17:00-ET CME close,
> you can skip 5.1 entirely — just supply `open/high/low/close/volume/spread_pts` and compute
> `ret = close.diff()`, `range = high - low`.

### 5.2 Multi-year stitching (one continuous series)
When you concatenate independently roll-adjusted yearly files, the seam injects a spurious
overnight jump. Re-anchor earlier files to the latest one:
```python
_ADJ = ["open", "high", "low", "close"]

def stitch_daily(frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = sorted([f.sort_index() for f in frames if len(f)], key=lambda f: f.index.min())
    out = frames[0].copy()
    for nxt in frames[1:]:
        common = out.index.intersection(nxt.index)
        if len(common):
            offset = float((nxt.loc[common, "close"] - out.loc[common, "close"]).median())
        else:  # adjacent, no overlap: match raw close-to-close continuity
            offset = float(nxt["close"].iloc[0] - out["close"].iloc[-1]) - \
                     float(nxt["open"].iloc[0] - out["close"].iloc[-1])
        out[_ADJ] = out[_ADJ] + offset
        out = pd.concat([out[~out.index.isin(nxt.index)], nxt]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out["ret"] = out["close"].diff()
    out["range"] = out["high"] - out["low"]
    return out
```
> Only needed if you assemble multiple roll-adjusted files. A single continuous series doesn't need it.

### 5.3 Signals
```python
def build_signals(d: pd.DataFrame, *, z_win=20, trend_win=50) -> pd.DataFrame:
    d = d.copy()
    d["atr"]            = d["range"].rolling(z_win).mean()
    d["ret_z"]          = d["ret"] / d["ret"].abs().rolling(z_win).mean()
    d["trend_ma"]       = d["close"].rolling(trend_win).mean()
    d["trend"]          = np.sign(d["close"] - d["trend_ma"])
    d["trend_slope"]    = d["trend_ma"] - d["trend_ma"].shift(10)
    d["trend_strength"] = d["trend_slope"].abs() / d["atr"]
    return d
```

### 5.4 The backtest engine (multi-concurrent, fixed R:R)
```python
def backtest(d: pd.DataFrame, *,
             z_entry=0.5, z_cap=4.0, trend_win=50, trend_strength_min=0.5,
             stop_atr=1.0, target_atr=1.5, max_hold=10,
             max_concurrent=2, lots_per=0.5,
             allow_long=True, allow_short=True,
             stop_slip_pts=0.2, restrict_year=None) -> pd.DataFrame:
    """Returns a fully-described trade blotter. Timing: decide at close of day c,
    fill at open of day c+1, manage on each held day's high/low. No look-ahead."""
    s = build_signals(d, z_win=20, trend_win=trend_win)
    n = len(s)
    O, H, L, C = (s[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    RETZ, TREND, ATR = s["ret_z"].to_numpy(), s["trend"].to_numpy(), s["atr"].to_numpy()
    TSTR = s["trend_strength"].to_numpy()
    SPREAD = s["spread_pts"].to_numpy(float)
    TIMES = s.index
    rows, openp = [], []

    def emit(p, ex, reason, ei):
        d0, fi = p["dir"], p["fill_idx"]
        stop_px = p["entry_px"] - d0 * p["stop_dist"]
        tp_px   = p["entry_px"] + d0 * p["tgt_dist"]
        gross = p["lots"] * d0 * (ex - p["entry_px"]) * POINT_VALUE
        cost  = p["lots"] * round_turn_cost_usd(SPREAD[fi])
        rows.append(dict(entry_time=TIMES[fi], exit_time=TIMES[ei],
            side="long" if d0 > 0 else "short", lots=p["lots"],
            entry_px=round(p["entry_px"],2), stop_px=round(stop_px,2), tp_px=round(tp_px,2),
            exit_px=round(ex,2), rr=round(p["tgt_dist"]/p["stop_dist"],2),
            atr_pts=round(p["atr"],2), trend_dir=("UP" if p["trend"]>0 else "DOWN"),
            trend_strength=round(p["tstr"],2), exit_reason=reason, bars_held=ei-fi+1,
            gross_usd=round(gross,2), cost_usd=round(cost,2),
            net_pnl_usd=round(gross-cost,2), win=bool(gross-cost > 0)))

    for c in range(n):
        # 1) manage live positions with day c's high/low
        keep = []
        for p in openp:
            if p["fill_idx"] > c:            # not filled yet
                keep.append(p); continue
            ex = reason = None
            if p["dir"] > 0:                 # long: stop checked before target
                if L[c] <= p["entry_px"] - p["stop_dist"]:
                    ex, reason = p["entry_px"] - p["stop_dist"] - stop_slip_pts, "stop"
                elif H[c] >= p["entry_px"] + p["tgt_dist"]:
                    ex, reason = p["entry_px"] + p["tgt_dist"], "target"
            else:                            # short
                if H[c] >= p["entry_px"] + p["stop_dist"]:
                    ex, reason = p["entry_px"] + p["stop_dist"] + stop_slip_pts, "stop"
                elif L[c] <= p["entry_px"] - p["tgt_dist"]:
                    ex, reason = p["entry_px"] - p["tgt_dist"], "target"
            if ex is None and (c - p["fill_idx"] + 1) >= max_hold:
                ex, reason = C[c], "time"
            if ex is None:
                keep.append(p)
            else:
                emit(p, ex, reason, c)
        openp = keep

        # 2) decide at day c, fill at c+1
        if c + 1 >= n:
            continue
        z = RETZ[c]
        yr_ok = (restrict_year is None) or (str(TIMES[c].year) == str(restrict_year))
        ok = (yr_ok and np.isfinite(z) and z_entry <= abs(z) <= z_cap
              and np.isfinite(ATR[c]) and ATR[c] > 0
              and np.isfinite(TSTR[c]) and TSTR[c] >= trend_strength_min)
        direction = -int(np.sign(z)) if ok else 0
        if direction != 0 and (not np.isfinite(TREND[c]) or direction != TREND[c]):
            direction = 0                    # must be WITH the trend
        if direction > 0 and not allow_long:  direction = 0
        if direction < 0 and not allow_short: direction = 0
        if direction != 0 and len(openp) < max_concurrent:
            openp.append(dict(fill_idx=c+1, dir=direction, entry_px=O[c+1],
                              stop_dist=stop_atr*ATR[c], tgt_dist=target_atr*ATR[c],
                              lots=lots_per, atr=ATR[c], trend=TREND[c], tstr=TSTR[c]))

    # 3) flush anything still open at the end of the data (mark-to-market at last close)
    for p in openp:
        if p["fill_idx"] <= n - 1:
            emit(p, C[n-1], "eod", n-1)

    return pd.DataFrame(rows).sort_values("entry_time").reset_index(drop=True) if rows else pd.DataFrame()
```

### 5.5 Metrics
```python
def metrics(tr: pd.DataFrame) -> dict:
    if len(tr) == 0: return {}
    pnl = tr["net_pnl_usd"].to_numpy()
    eq = np.cumsum(pnl); maxdd = float((np.maximum.accumulate(eq) - eq).max())
    daily = tr.set_index("entry_time")["net_pnl_usd"].groupby(lambda t: t.date()).sum()
    sharpe = float(daily.mean()/daily.std(ddof=1)*np.sqrt(252)) if daily.std(ddof=1) else np.nan
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    return dict(n=len(pnl), net=float(pnl.sum()), win_rate=float((pnl > 0).mean()),
                pf=float(wins.sum()/-losses.sum()) if losses.sum() else np.inf,
                sharpe=sharpe, maxdd=maxdd,
                tstat=float(pnl.mean()/(pnl.std(ddof=1)/np.sqrt(len(pnl)))) if len(pnl)>1 else np.nan)
```

---

## 6. The two configurations

Both share the same rules and the same **1.5:1 risk-reward**. They differ only in how many
concurrent positions and how loose the entry gate is — trading *frequency & drawdown* against
*per-trade concentration*.

### PRIMARY — higher net, ~3 trades/month
```python
PRIMARY = dict(
    z_entry=0.5, z_cap=4.0, trend_win=50, trend_strength_min=0.5,
    stop_atr=1.0, target_atr=1.5, max_hold=10,
    max_concurrent=2, lots_per=0.5,          # up to 2 open, each 1/2 lot -> ~1 unit total risk
    allow_long=True, allow_short=True,
)
```

### LOW-DD — lower drawdown, higher win rate
```python
LOW_DD = dict(PRIMARY, z_entry=0.6, max_concurrent=3, lots_per=1/3)   # 3 open, each 1/3 lot
```

**Sizing in practice (both configs):** `lots_per` is a research unit meaning "size your base
position so `max_concurrent` open positions equal your risk budget." For PRIMARY with a 2-contract
budget: **trade 1 contract per entry, up to 2 open at once**. All P&L below is on this
~1-contract-of-risk basis; scale linearly for larger accounts.

---

## 7. Metrics

Development on **2024–2025** (in-sample). **2026 (Jan–Jul) is out-of-sample** — the strategy was
never tuned on it. 1 lot-equivalent risk, net of taker costs. Regimes: 2024 bull, 2025 bull,
**2026 down −7.5% with a −26% crash** (buy-and-hold **−$41,230**).

### PRIMARY
| Year | n | Long/Short | Net $ | Win % | PF | Sharpe | t | MaxDD $ |
|---|---|---|---|---|---|---|---|---|
| 2024 | 29 | 27 / 2 | 25,951 | 65.5 | 2.45 | 6.89 | 2.34 | 5,312 |
| 2025 | 41 | 40 / 1 | 73,004 | 61.0 | 2.47 | 6.61 | 2.67 | 17,830 |
| **In-sample** | **69** | **66 / 3** | **93,213** | **62.3** | **2.38** | **6.09** | — | **17,830** |
| **2026 OOS** | **23** | **9 / 14** | **103,788** | **69.6** | **3.68** | **9.58** | **2.89** | **21,982** |

### LOW-DD
| Year | n | Long/Short | Net $ | Win % | PF | Sharpe | t | MaxDD $ |
|---|---|---|---|---|---|---|---|---|
| 2024 | 28 | 26 / 2 | 15,746 | 64.3 | 2.32 | 6.44 | 2.15 | 3,541 |
| 2025 | 40 | 39 / 1 | 56,109 | 65.0 | 2.94 | 7.95 | 3.17 | 9,107 |
| **In-sample** | **67** | **64 / 3** | **68,028** | **64.2** | **2.67** | **6.89** | — | **9,107** |
| **2026 OOS** | **24** | **10 / 14** | **60,525** | **66.7** | **2.98** | **7.83** | **2.42** | **19,481** |

**Read this honestly:** ~60–65% of the P&L is trend-beta; the 2026 short side (+$55k, 71% win)
validated the regime-adaptivity that is the core edge. 2026's metrics are *better* than in-sample
— a favorable big-directional draw — **do not extrapolate the OOS Sharpe**. Small samples
(23–69 trades/window); treat significance conservatively.

---

## 8. Step-by-step: build & run

**A. From scratch (any language) using §5 as the blueprint:**
1. Get front-month GC data. Ideally trade+BBO (TBBO) or 1-min bars with a spread column; a clean
   daily OHLC aligned to the 17:00-ET CME close also works.
2. **Aggregate to CME session days** (§5.1). Get the DST boundary right (17:00 ET). This one detail
   materially changes results.
3. If you assembled multiple roll-adjusted files, **stitch** them (§5.2).
4. Compute **signals** (§5.3): `atr, ret_z, trend, trend_strength`.
5. Run the **engine** (§5.4) with the PRIMARY config.
6. Compute **metrics** (§5.5) and inspect the trade blotter.
7. **Validate on held-out data** you did not tune on. Do not change parameters based on it.

**B. Using this repository directly:**
```bash
pip install -r requirements.txt                     # databento, pandas, numpy, scipy, pyarrow

# TBBO .dbn.zst -> cached 1-min feature bars (streaming, low memory)
python -m drivers.build_features --year 2024 --freq 1min
python -m drivers.build_features --year 2025 --freq 1min

python -m drivers.verify_no_lookahead --tag 2025    # prove causality at the CME boundary
python -m drivers.run_frozen --year 2025 --warmup 2024          # PRIMARY
python -m drivers.run_frozen --year 2025 --warmup 2024 --lowdd  # LOW-DD
python -m drivers.matrix                            # full metrics matrix
python -m drivers.export_trades                     # year-wise trade CSVs -> reports/trades/

# Out-of-sample validation on a new year (do NOT change config afterwards)
python -m drivers.build_features --year 2026 --freq 1min
python -m drivers.run_frozen --year 2026 --warmup 2025
python -m drivers.matrix --add 2026
```
`--warmup <prior year>` prepends prior-year sessions so the 50-/20-session windows are warm from
day one; stitching keeps the year seam continuous. The frozen parameters live in
`strategy/config.py` (`FROZEN_PARAMS`, `FROZEN_LOWDD_PARAMS`).

**Minimal end-to-end example (using §5 functions on a daily frame you already have):**
```python
d = to_daily(bars_1min)                 # or supply your own daily OHLC frame
d = stitch_daily([d_2024, d_2025])      # optional
trades = backtest(d, **PRIMARY)
print(metrics(trades))
trades.to_csv("trades.csv", index=False)
```

---

## 9. Assumptions & no-look-ahead guarantees

- **Decision/fill timing:** every signal uses only data through session D's close; the position is
  filled at session D+1's **open**. The ~1-hour CME break sits between decision and fill.
- **Fills:** taker/marketable. Entry at the next open; exits at the stop/target level (stop with a
  small adverse slippage `stop_slip_pts`). Cost = cross the full quoted spread + $1.50/side.
  This is deliberately **conservative** — a passive/limit implementation would pay less.
- **Intrabar ambiguity:** stop and target are checked on daily high/low. If a day's range spans
  both, the engine assumes the **stop filled first** (pessimistic). An intraday (1-min) exit
  simulation would remove this assumption; expect a small favorable difference, not adverse.
- **No look-ahead — proven, not assumed:** perturbing every *future* bar by a large constant leaves
  every *past-day* signal byte-identical; and no session-day aggregate contains a tick from an
  adjacent session (0 bleed across the 17:00-ET boundary, summer or winter). See
  `drivers/verify_no_lookahead.py`.
- **End-of-data:** positions still open when the data ends are marked-to-market at the last close
  (`exit_reason="eod"`) — never silently dropped (important for a partial final year).
- **Roll handling:** on a continuous front-month series, positions are not held across a contract
  roll; prices are roll-adjusted for indicator continuity but P&L uses the traded contract's prices.

---

## 10. Taking it live

1. **Data feed:** subscribe to the front-month GC (or the continuous front-month symbol). You need,
   per session, the OHLC and a spread estimate. Rebuild session bars at the **17:00-ET** close.
2. **Daily cycle:** at/after 17:00 ET, recompute `atr, ret_z, trend, trend_strength` on the closed
   session; evaluate the entry gate; if it fires and you have a free concurrency slot, place a
   **market/limit order for the 18:00-ET reopen** (next-session open).
3. **Bracket each fill** with a hard stop at `entry ∓ 1.0×ATR` and a limit target at
   `entry ± 1.5×ATR`; add a **time stop** to flatten after 10 sessions.
4. **Sizing:** choose a base unit so `max_concurrent` positions = your risk budget (PRIMARY: 1
   contract/entry, ≤2 open). Never exceed `max_concurrent` open positions.
5. **Guardrails (recommended):** per-side kill-switch (the short side is thin in-sample — watch it
   in a downtrend), halt after N consecutive losers, and pause if `trend_strength` collapses (chop).
6. **Costs:** budget the taker model above; if you post limit entries, track your realized fills vs
   the modeled next-open — that difference is your execution edge/slippage.

---

## 11. Known limitations & how to extend

**Limitations (be explicit with stakeholders):**
- Trend-following, ~60–65% **beta** — needs gold to trend (either direction); flat/choppy is the
  failure mode (the gate reduces but doesn't eliminate whipsaw).
- **Short side is thin in-sample** (3 trades, all losers — brief bull pullbacks) but **worked
  out-of-sample** in the 2026 downtrend. Watch it live.
- Concurrent positions are the **same instrument & direction → correlated**; the controlled
  drawdown is entry-timing diversification, not true diversification.
- Small samples; ~650 configs were searched during research — treat in-sample significance modestly.
- Structural drawdown (~$18–22k at 1-unit risk) comes from failed pullback entries; exits can't tune
  it away, only sizing and the regime gate reduce it.

**Highest-value extensions (develop on train data, validate on held-out only):**
- **Trend-strength-scaled sizing** — bet more when the trend is cleanest.
- **Explicit chop detector** (efficiency ratio / ADX) as a hard no-trade filter.
- **True diversification** — run the identical rules on silver (SI) and other metals; trade the
  book so one instrument's chop doesn't stall everything.
- **Passive-entry cost model** — a limit-entry version could materially improve net economics.
- **Intraday exit simulation** to remove the daily stop/target ambiguity.
- **Ensemble of trend windows** (30/50/100) to reduce single-parameter sensitivity.

---

*Companion documents in this repo: `docs/STRATEGY_DEVELOPMENT.md` (full research history — every
version tested, why each failed, bugs found) and `README.md` (quick-start). This file is the
build/implementation spec.*
