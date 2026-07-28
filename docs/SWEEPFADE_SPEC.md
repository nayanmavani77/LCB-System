# BIG-Sweep Fade — Implementation Specification

**Purpose:** a complete, self-contained specification for building a live signal/execution
engine and a backtester. This document assumes no prior context. Everything needed to
implement and verify the strategy is contained here.

**Status:** validated on 30 hours of GC tbbo data (2 CME sessions, 351 BIG events,
98 trades). Statistically significant but small-sample — see §11.

---

## 1. Strategy in one paragraph

A "BIG sweep" is a single large market order that walks through multiple price levels in
milliseconds because the order book is too thin to absorb it. The resulting price
displacement is a **liquidity artefact, not price discovery**: market makers re-quote
around pre-sweep fair value within seconds and price retraces most of the move. The
strategy therefore **trades against** every qualifying sweep, holds for up to 10 minutes,
and exits on a fixed target, a volatility-scaled stop, or a hard clock.

Trading *with* sweeps loses −0.335 R/trade (t = −2.74). Trading *against* them makes
+0.355 R/trade (t = +2.52).

---

## 2. Instrument and units

| Item | Value |
|---|---|
| Instrument | GC — COMEX Gold futures (continuous front month, `GC.v.0`) |
| Contract size | 100 troy oz |
| Minimum tick | 0.10 price units = **$10.00** per contract |
| 1 "point" | 1.00 price unit = 10 ticks = **$100.00** per contract |
| Typical spread | 3 ticks (0.30) — median; p90 = 5 ticks |
| Typical volume | 40 contracts/minute (median) |

**Internal convention:** perform all P&L maths in **ticks**, convert for display.
`ticks = price_difference / 0.10`. Never store P&L as a float price difference.

To port to another instrument, change only `TICK` and `TICK_VALUE`. All thresholds
expressed in ticks are instrument-specific and must be re-derived.

---

## 3. Input data format

The engine consumes a line-oriented text log. Four record types.

### 3.1 Trade print
```
11:49:18.218  SELL      1 @ 4097.70   CVD         -1  [4097.70/4097.90]
```
Regex:
```
^(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s+(BUY|SELL|\?)\s+(\d+)\s+@\s+([\d.]+)\s+
CVD\s+([+-]?\d+)\s+\[([\d.]+)/([\d.]+)\]
```
Fields: time-of-day, aggressor side (`?` = unclassified, ~11.8% of prints), size,
price, running CVD, bid, ask. **The bid/ask is the BBO immediately *before* the trade.**

### 3.2 One-minute bar
```
[1m 11:50] O 4097.50 H 4099.30 L 4097.40 C 4099.20 | vol 36 delta +11 | CVD +12 | 28 trades
[1m 12:10] O 4096.20 H 4096.20 L 4092.60 C 4093.40 | vol 201 delta -89 | CVD -141 | 118 trades | big 0B/2S -83
```
Regex:
```
^\[(\d+)m\s+(\d{2}):(\d{2})\]\s+O\s+([\d.]+)\s+H\s+([\d.]+)\s+L\s+([\d.]+)\s+C\s+([\d.]+)\s+\|
\s+vol\s+(\d+)\s+delta\s+([+-]?\d+)\s+\|\s+CVD\s+([+-]?\d+)\s+\|\s+(\d+)\s+trades
(?:\s+\|\s+big\s+(\d+)B/(\d+)S\s+([+-]?\d+))?
```
The trailing `big xB/yS net` group is optional and only appears after the first BIG event.
**Bars are not used by the strategy** — parse them for reporting only.

### 3.3 BIG event — the signal trigger

Two variants:
```
>>> BIG SELL sweep 52 @ 4093.60->4092.60 in 0ms (n=16, 31x avg)  CVD -146
>>> BIG BUY  print 20 @ 4077.70 (n=1, 12x avg)  CVD -766
```
Regex (sweep):
```
^>>>\s+BIG\s+(BUY|SELL)\s+sweep\s+(\d+)\s+@\s+([\d.]+)->([\d.]+)\s+in\s+(\d+)ms\s+
\(n=(\d+),\s+([\d.]+)x avg\)\s+CVD\s+([+-]?\d+)
```
Regex (print):
```
^>>>\s+BIG\s+(BUY|SELL)\s+print\s+(\d+)\s+@\s+([\d.]+)\s+\(n=(\d+),\s+([\d.]+)x avg\)\s+
CVD\s+([+-]?\d+)
```

| Field | Meaning |
|---|---|
| `side` | aggressor direction of the sweep (BUY = someone lifted offers) |
| `size` | total contracts in the sweep |
| `p_start` → `p_end` | first and last execution price. **`p_end` is the reference price for everything.** |
| `dur_ms` | duration; 39% are 0 ms, 89% ≤ 10 ms |
| `n_prints` | number of individual prints / price levels consumed |
| `x_avg` | sweep size as a multiple of recent average trade size |
| `CVD` | running cumulative volume delta after the sweep |

**Note:** `>>> BIG` lines carry **no timestamp**. Timestamp them with the timestamp of the
**most recent preceding trade print**.

### 3.4 Session markers
```
--- new CME session (CVD reset) ---
=== session done: vol 43,056, CVD -577, 28895 trades | big: 71 buys +2,360 / 71 sells -2,575 ===
```
Increment a session counter on `--- new CME session`. CVD resets at that boundary.

---

## 4. Parsing requirements

### 4.1 Clock reconstruction (mandatory)
Timestamps are **time-of-day only**, and a log may span multiple days. Build a monotonic
clock: maintain a day counter; when a new time-of-day is **more than 3600 seconds earlier**
than the previous one, increment the day. Never sort by the raw time-of-day string.

### 4.2 Preserve file order — do not re-sort
Records are already chronological. A sweep places **many prints in the same millisecond**
(up to 61 observed). Any non-stable sort scrambles their order and silently corrupts
results. If sorting is unavoidable use a **stable** sort keyed on original file position.

### 4.3 Use `p_end`, never a positional lookup
Because many prints share one millisecond, "the first print at time *t*" is undefined.
All entry/return maths must reference the sweep's stated `p_end` from the alert line.

### 4.4 Record the source line number
Two BIG events can resolve to the same timestamp. Give every event a unique id and store
its log line number for traceability.

---

## 5. Signal specification

Evaluate on **every** `>>> BIG` line, in real time, using only information available at or
before that moment.

### 5.1 Hard gate
```
IF kind != "sweep"  ->  REJECT
```
A `print` is a single fill at one price — a counterparty knowingly absorbed the whole
order. Those **continue** (fading them returned −16.1 ticks at 5 min, 29% win rate).
Never trade them in either direction.

### 5.2 Quality conditions — compute all three

```
c_magnitude  =  (x_avg >= 18.0)  OR  (size >= 35)
c_multilevel =  (n_prints >= 6)
c_clean      =  (number of OPPOSITE-side BIG events in the prior 5 minutes == 0)
qscore       =  c_magnitude + c_multilevel + c_clean          # integer 0..3
```

Notes:
- `c_clean` counts BIG events of **either kind** (sweep or print) whose `side` differs
  from the current event's `side`, with timestamp in `[now − 5 min, now)`. Strictly
  backward-looking; exclude the current event.
- `c_multilevel` is the strongest single filter. Sweeps with `n_prints ≤ 5` returned
  **−5.86 ticks** at 10 minutes — losing outright.

### 5.3 Decision
```
IF qscore >= 2  AND  no position currently open  ->  TRADE
ELSE                                             ->  SKIP
```

`qscore == 2` → standard conviction. `qscore == 3` → high conviction (affects size only).

**Do not** use `qscore >= 3` as the entry threshold. It halves the sample and was unstable
across sessions.

### 5.4 Direction
```
sweep side BUY   ->  go SHORT
sweep side SELL  ->  go LONG
```

---

## 6. Order and execution specification

### 6.1 Entry — MARKET order, immediately

No delay, no limit order, no waiting for confirmation. This is the most heavily tested
decision in the strategy; every alternative was worse:

| Entry method | R per signal |
|---|---|
| **Market, immediately** | **+0.257** |
| Limit at sweep extreme (78% fill) | +0.135 |
| Limit 1 tick into the sweep (72% fill) | +0.109 |
| Limit 2 ticks into the sweep (67% fill) | +0.091 |
| Wait 15 s then enter | +0.205 |
| Wait 30 s then enter | +0.142 |
| Wait 60 s then enter | +0.095 |
| Wait for price to reclaim `p_start` | +0.091 |

Cause: **adverse selection.** Signals a resting limit *missed* were worth +0.905 R
(74% win); signals it *filled* were worth +0.078 R (45% win). A limit only fills when the
reversion fails.

For backtesting, model the fill as:
```
entry_price = p_end + direction_sign * ENTRY_SLIP_TICKS * TICK
              # direction_sign = +1 for LONG, -1 for SHORT  (i.e. ALWAYS adverse)
ENTRY_SLIP_TICKS = 2.0     # default assumption
```

### 6.2 Stop — volatility-scaled, stop-MARKET order
```
range_5m_ticks = (max(trade price) − min(trade price)) over the trailing 300 seconds / TICK
stop_ticks     = clamp(0.50 * range_5m_ticks, 8, 45)
stop_price     = entry_price − direction_sign * stop_ticks * TICK
```
The trailing window is **trade prints only**, ending at the signal timestamp inclusive.

Use a **stop-market**, never a stop-limit: a stop-limit can go unfilled during exactly the
liquidation you need to escape.

Observed distribution: p10 = 12 ticks ($124), median = 23 ticks ($230), p90 = 42 ticks ($415).

### 6.3 Target — resting LIMIT order
```
target_ticks = stop_ticks * 2.5
target_price = entry_price + direction_sign * target_ticks * TICK
```
Place immediately on fill. No spread cost is paid on this exit.

### 6.4 Time stop — MARKET order
```
IF elapsed_since_entry >= 600 seconds  ->  flatten at market
```
This is a **primary exit**, not a fallback — it closes 43% of all trades, more than the
stop does. Implementations that treat it as an afterthought will not reproduce the results.

### 6.5 Exit precedence
Within a single price update, evaluate in this order:
1. Stop hit → exit at `stop_price`, charge `EXIT_SLIP_TICKS`
2. Target hit → exit at `target_price`, charge nothing
3. Time expired → exit at last price, charge `EXIT_SLIP_TICKS`

Stop is checked **first**. This is intentionally conservative: when a single bar or tick
window contains both levels, assume the loss.

---

## 7. Position and risk management

### 7.1 One position at a time
Ignore any signal that fires while a position is open. Signals cluster heavily — four fired
within 10 seconds in the sample. Enforcing this reduced 180 signals to 98 trades while
**raising** expectancy from 0.257 R to 0.355 R and cutting max drawdown from 8.9 R to 6.0 R.

Do **not** implement a cooldown period after a trade closes; that tested worse.

### 7.2 Sizing
```
risk_pct  = 0.010  if qscore == 3  else  0.005      # of account equity
contracts = round( equity * risk_pct / (stop_ticks * TICK_VALUE) )
```
Contract count **must** vary with `stop_ticks`. A fixed lot size makes the widest-stop
trades the largest losers.

qscore-3 setups fire in calmer tape so their stops are smaller (2.15 vs 2.74 points). They
produce fewer points per trade but **more R per unit risked** (0.415 vs 0.301). Because
sizing is risk-normalised, R is the correct basis — hence the larger allocation.

### 7.3 Trade management — none

Once filled: stop, target, clock. Nothing else. Every overlay tested made it worse:

| Overlay | Expectancy | vs. baseline |
|---|---|---|
| **Plain** | **+0.257 R** | — |
| Breakeven stop after +10 ticks | +0.008 R | −97% |
| Breakeven stop after +15 ticks | −0.006 R | −102% |
| Scale 50% at +12 ticks, rest to breakeven | −0.091 R | −135% |
| Trail 0.75 R after +1 R | +0.062 R | −76% |
| Trail 1 R after +2 R | +0.116 R | −56% |

Reason: median adverse excursion is 19 ticks **even on winners**, against a median
favourable excursion of 35. Any stop that tightens after a small gain is picked off by the
noise the trade exists to harvest.

**Do not implement breakeven stops, trailing stops, or partial exits.**

### 7.4 Session limits (convention, not derived from data)
- Daily stop: −4 R
- No daily profit target
- After 5 consecutive losses: pause 1 hour

---

## 8. Cost model

| Cost | Ticks | Applied to |
|---|---|---|
| `ENTRY_SLIP_TICKS` | 2.0 | every trade — worsens the entry price |
| `EXIT_SLIP_TICKS` | 1.5 | stop and time exits only (market orders) |
| `COMMISSION_TICKS` | 0.5 | every trade, round turn (≈ $5) |

```
net_ticks = gross_ticks − COMMISSION_TICKS        # exit slippage already inside gross
R         = net_ticks / stop_ticks
```

Costs consume **39% of the gross edge**. Direct cost is 0.371 points/trade but measured
drag is 0.531 — the difference is a path effect: a worse entry shifts the stop and target
with it, so some trades that would have hit the target instead stop or time out.

**Break-even is ~5–6 ticks of entry slippage.** Make `ENTRY_SLIP_TICKS` a configurable
parameter and expose a sensitivity report; this is the strategy's single point of failure.

---

## 9. Acceptance criteria

A correct implementation, run against a log with the properties in §10, must reproduce
these. Numbers are for the reference 30-hour GC sample.

### 9.1 Parsing
| Quantity | Expected |
|---|---|
| Trade prints | 79,142 |
| 1-minute bars | 1,739 |
| BIG events | 351 (344 sweeps + 7 prints) |
| Sessions | 2 |
| Side-unclassified prints | 11.8% |

### 9.2 Signal generation
| Quantity | Expected |
|---|---|
| Qualifying signals (`qscore ≥ 2`, sweeps only) | 180 |
| Trades after one-position-at-a-time filter | 98 |
| qscore 2 / qscore 3 split | 52 / 46 |
| LONG / SHORT split | 57 / 41 |

### 9.3 Backtest — default parameters, default costs
| Metric | Expected |
|---|---|
| Expectancy | +8.36 ticks = +0.836 points = **+$83.57** |
| Expectancy in R | **+0.355 R** |
| Win rate | **54.1%** (53W / 45L) |
| Profit factor | **1.81** |
| Total P&L, 1 contract flat | +81.90 points = **+$8,190** |
| t-statistic | **+2.52** |
| Max drawdown | −6.0 R = −14.70 points |
| Average stop distance | 24.6 ticks = $246 |
| Average hold | 6.3 minutes |
| Exit mix | time 42, stop 37, target 19 |

### 9.4 Inverse control (critical sanity check)
Running the identical logic with the direction **flipped** must produce approximately:
| Metric | Expected |
|---|---|
| Trades | 106 |
| Expectancy | **−0.335 R** |
| Win rate | 24.5% |
| Profit factor | 0.48 |

If the inverse is not strongly negative, the implementation has a sign error or a
lookahead bug.

### 9.5 Sensitivity — shapes that must hold
- Entry slippage 0 → 6 ticks: expectancy falls monotonically +0.51 R → +0.04 R
- Max hold 3 / 5 / 10 / 15 / 30 min: +0.05 / +0.16 / **+0.26** / +0.24 / +0.22 R (peak at 10)
- Stop multiplier 0.3 → 1.0 × range: broad plateau, best 0.5–0.75
- Target 1.5 R → 4 R: flat, no sharp optimum

---

## 10. Live engine requirements

```
on_big_event(event):
    if event.kind != "sweep":                     return
    if position_open():                           return

    qscore = (event.x_avg >= 18 or event.size >= 35) \
           + (event.n_prints >= 6) \
           + (count_opposite_big(last_5_minutes) == 0)
    if qscore < 2:                                return

    direction  = SHORT if event.side == "BUY" else LONG
    stop_ticks = clamp(0.5 * range_5m_ticks(), 8, 45)
    risk_pct   = 0.010 if qscore == 3 else 0.005
    qty        = round(equity * risk_pct / (stop_ticks * 10))
    if qty < 1:                                   return

    fill = send_market_order(direction, qty)
    send_stop_market(opposite(direction), qty, fill - sign(direction)*stop_ticks*TICK)
    send_limit      (opposite(direction), qty, fill + sign(direction)*stop_ticks*2.5*TICK)
    schedule_flatten_at_market(now + 600)

    log(event_id, signal_ts, order_ts, assumed_entry=event.p_end,
        actual_fill=fill, stop, target, qty, latency_ms)
```

**Required state:** rolling 5-minute buffer of trade prices (for the volatility stop),
rolling 5-minute buffer of BIG events with side (for `c_clean`), current position.

**Latency budget:** signal-to-order under ~200 ms. The engine must log
`assumed_entry` vs `actual_fill` on every trade — this is not optional
instrumentation, it is the measurement that determines viability.

---

## 11. Known limitations

1. **98 trades from 30 hours of one instrument.** t = 2.52 is significant but this is a
   hypothesis, not a validated system. Bootstrap 95% CI on expectancy: **[+0.076, +0.628] R**.
2. **Two sessions is not a regime sample.** Both were net-downtrending with elevated
   volatility. Quiet and range-bound regimes are unmeasured.
3. **Execution dominates.** A ~5-tick gross edge against a 3-tick spread.
4. **No fill modelling.** Partial fills, queue position, and book movement during order
   transit are not simulated.
5. **Side classification is imperfect** — 11.8% unclassified prints add noise to CVD.
6. **Stop slippage may be understated** at 1.5 ticks.
7. **Cross-instrument transfer untested.** The mechanism should strengthen in thinner
   books and weaken in deeper ones.

---

## 12. Explicitly out of scope — do not implement

These were tested and rejected. Adding them will degrade results.

| Feature | Why rejected |
|---|---|
| Breakeven stops | −97% expectancy |
| Trailing stops | −56% to −76% |
| Partial profit-taking | −135% |
| Confirmation delay of any length | monotonically worse with delay |
| Limit / pending entry orders | adverse selection, −47% |
| Cooldown after a closed trade | worse than no cooldown |
| Trend-alignment filter | flipped sign between sessions |
| Time-of-day filters | 15–25 observations per bucket, overfit |
| Volume-derived filters (14 tested: 1m/5m volume, volume ratio, participation rate, absorption, order-flow imbalance, bar delta, volume z-score) | **0 of 14 reached \|t\| ≥ 2; every one reduced total P&L** |
| `qscore ≥ 3` as entry threshold | halves sample, unstable across sessions |

---

## 13. Parameter reference

```python
# Instrument
TICK              = 0.10
TICK_VALUE        = 10.00

# Costs (ticks)
ENTRY_SLIP_TICKS  = 2.0
EXIT_SLIP_TICKS   = 1.5
COMMISSION_TICKS  = 0.5

# Signal
REQUIRE_SWEEP     = True
MIN_QSCORE        = 2
MAG_X_AVG         = 18.0
MAG_SIZE          = 35
MIN_PRINTS        = 6
CLEAN_WINDOW_MIN  = 5

# Risk
ATR_WINDOW_SEC    = 300
STOP_ATR_MULT     = 0.50
STOP_MIN_TICKS    = 8
STOP_MAX_TICKS    = 45
TARGET_R          = 2.5
MAX_HOLD_SEC      = 600

# Portfolio
SEQUENTIAL        = True
RISK_PCT          = {2: 0.005, 3: 0.010}   # qscore -> fraction of equity
```

---

## 14. Implementation pitfalls

1. **`sort_values` in pandas defaults to an unstable quicksort.** Sorting a tape with
   duplicate timestamps scrambles within-millisecond order and silently changes results.
   Do not sort; the log is already chronological.
2. **`series.size` in pandas returns the element count, not a column named `size`.**
   Always use `series["size"]`. This fails silently and returns a plausible integer.
3. **Midnight rollover** — a log spanning two days will appear to travel backwards in time
   unless the day counter is implemented (§4.1).
4. **BIG lines have no timestamp** — inherit from the preceding trade print (§3.3).
5. **Duplicate event timestamps** — two BIG events can share one timestamp. Key on a
   unique event id, never on the timestamp.
6. **Do not compute the entry from a positional tape lookup.** Use `p_end` (§4.3).
7. **Entry slippage sign** — it must always make the fill *worse*: higher for a long,
   lower for a short. Getting this backwards produces a plausible but inverted
   sensitivity curve (results improving with more slippage), which is the tell.
8. **The time stop must be a first-class exit path**, not a loop fall-through — it is 43%
   of all trades.
