# LCB-System — Command Reference

Every command runs from the project folder:

```
cd "C:\Users\nayan\Desktop\Claude Workspace\LCB-System"
```

---

## 1. One-time setup

| Command | What it does |
|---|---|
| `pip install databento pandas pyarrow zstandard numpy` | install everything the backtest needs |
| `pip install MetaTrader5` | install the MT5 bridge (live trading only) |
| create `config.py` in the project root (template below) | API key + optional MT5 login (config.py is gitignored — secrets stay private) |

`config.py` template:

```python
# --- Databento (required for live data) ------------------------------
DATABENTO_API_KEY = "db-PASTE-YOUR-KEY-HERE"

# --- MT5 account (OPTIONAL) -------------------------------------------
# Leave as None to attach to the account already logged in inside your
# running MT5 terminal (the usual way). Fill in only if you want the
# script to log the terminal in itself, e.g. for unattended restarts.
MT5_LOGIN = None          # e.g. 12345678  (account number)
MT5_PASSWORD = None       # e.g. "your-password"
MT5_SERVER = None         # e.g. your broker's server name
```

---

## 2. Data — download & prepare

### 2a. `scripts/download_data.py` — download from Databento into `Data\<SYMBOL>\`

| Option | Meaning | Default |
|---|---|---|
| `--symbol GC` | symbol from `core/symbols.py` (GC, SI, HG, PL, CL, NG) | GC |
| `--start 2026-07-18` | first date to download (**required**) | — |
| `--end 2026-09-01` | last date to download (**required**) | — |

```
python scripts/download_data.py --symbol GC --start 2026-07-18 --end 2026-09-01
```

### 2b. `scripts/prep.py` — build the tick/bar cache from the DBN files

| Option | Meaning | Default |
|---|---|---|
| `--symbol GC` | which symbol's `Data\<SYMBOL>\` folder to process (GC also reads the legacy flat `Data\`) | GC |

```
python scripts/prep.py                # GC
python scripts/prep.py --symbol SI    # another symbol
```

Run prep once after every download. Known symbols (add more in
`core/symbols.py`, one dict entry each): GC (gold), SI (silver), HG (copper),
PL (platinum), CL (WTI oil), NG (nat gas). Everything symbol-specific
(Databento symbols, $/point, default spread gate, default cost, default MT5
symbol) lives in that registry — the strategy code never changes per symbol.

---

## 3. Backtest — `run/backtest.py`

Basic form:

```
python run/backtest.py --start YYYY-MM-DD --end YYYY-MM-DD
```

### All backtest options

| Option | Meaning | Default |
|---|---|---|
| `--start` / `--end` | date window (clamped to available data) | all data |
| `--symbols GC,SI` (alias `--symbol`) | one symbol → full report; comma list → per-symbol reports + combined portfolio (joint $ P&L, joint max DD) | GC |
| `--engine` | `lrev` (validated level-break) / `gtrend` (daily trend-pullback) / `gtrend-lowdd` (same rules, lower drawdown) | lrev |
| `--config` | lrev gate presets: `v2-flow` (all gates) / `v2-ea` (no flow gate) / `v1` (no gates) | v2-flow |
| `--csv FILE.csv` | save every trade to a CSV (written into `logs\`; one file per symbol when multi-symbol) | off |
| `--cost 0.4` | commission+slippage per round turn in points (spread is separately embedded in fills) | per-symbol value from `core/symbols.py` |
| `--verbose` | print every level, gate decision and fill | off |
| *strategy flags* | all of section 4 (`--rr`, `--sl-*`, `--set KEY=VALUE`, ...) work here too | — |

Examples:

```
python run/backtest.py                                        # GC, everything, default strategy
python run/backtest.py --start 2026-04-01 --end 2026-07-17
python run/backtest.py --symbols SI --start 2025-01-01        # another symbol (cache required)
python run/backtest.py --symbols GC,SI --start 2025-01-01     # PARALLEL: per-symbol reports
                                                              # + combined portfolio ($, joint max DD)
python run/backtest.py --start 2023-01-01 --end 2026-01-01 --csv trades.csv
python run/backtest.py --config v1                            # original ungated system
python run/backtest.py --engine gtrend --start 2024-01-01     # daily trend-pullback engine
python run/backtest.py --engine gtrend-lowdd                  # same rules, LOW-DD sizing
```

All generated CSVs land in `logs\` (give an absolute path to override).

---

## 4. Strategy settings (SAME flags for backtest AND live)

Add these to `run/backtest.py` or `run/live.py` — identical meaning in both:

| Flag | Meaning | Default |
|---|---|---|
| `--rr 3.0` | take-profit = SL distance × RR (lrev) | 2.0 |
| `--sl-m15 1.0` | M15 stop = multiplier × median true range (lrev) | 1.5 |
| `--sl-h1 0.5` | H1 stop multiplier (lrev) | 0.5 |
| `--sl-h4 0.5` | H4 stop multiplier (lrev) | 0.5 |
| `--tf M15,H1` | trade only these timeframes (lrev) | M15,H1,H4 |
| `--max-spread 0.9` | skip triggers when spread > $X (0 = off) | per-symbol registry value |
| `--order-age 35` | cancel unfilled level after N hours (0 = off; lrev) | 35 |
| `--flow-lo 0.0` `--flow-hi 0.6` | flow-gate band (aligned 30s imbalance; lrev) | 0.0–0.6 |
| `--set KEY=VALUE` | override ANY engine config key (repeatable) — e.g. `--set allow_short=false`. Works for every engine; keys live in each engine file's CONFIG dict | — |

Section 5 lists EVERY engine's own settings and how to change them.

Workflow: validate a setting in backtest, then run live with the *exact same flags*:

```
python run/backtest.py --start 2026-01-01 --rr 3.0 --sl-m15 1.0
python run/live.py --broker mt5 --mt5-symbol XAUUSD+ --lots 0.01 --rr 3.0 --sl-m15 1.0
```

Every run prints its configuration first (`strategy: RR 3.0 | ...`) so you
always know what's being tested or traded.

---

## 5. Engine-specific settings (all changeable via `--set KEY=VALUE`)

Each engine's parameters live in a CONFIG dict at the top of its file.
Any key can be overridden per run with `--set KEY=VALUE` (repeatable), in
backtest AND live. Keys with a dedicated flag (noted below) can use either.

### 5a. `lrev` — level-break engine (validated)

| Key | Meaning | Default |
|---|---|---|
| `rr` | TP = SL distance × RR *(flag: `--rr`)* | 2.0 |
| `fractal_bars` | bars each side of a swing to confirm a level | 8 |
| `max_level_distance` | ignore levels further than $X from price | 200 |
| `level_max_age_h` | drop levels older than N hours | 336 |
| `order_max_age_h` | gate 1: cancel unfilled level after N h *(flag: `--order-age`)* | 35 |
| `max_spread` | gate 2: skip when spread > $X *(flag: `--max-spread`)* | per-symbol |
| `use_flow_gate` | gate 3 on/off *(flag: `--no-flow-gate` in live)* | true |
| `flow_lo` / `flow_hi` | flow-gate band *(flags: `--flow-lo/hi`)* | 0.0 / 0.6 |
| `flow_window_s` | flow imbalance window (seconds) | 30 |
| `qty` | contracts per backtest fill | 1 |

SL multipliers per timeframe: `--sl-m15 / --sl-h1 / --sl-h4` (1.5/0.5/0.5),
timeframe selection: `--tf M15,H1`.

### 5b. `gtrend` / `gtrend-lowdd` — daily trend-pullback (GC only)

Parameters were FROZEN on 2024-2025 GC data — change them only for
research, never for live, and never tune on your validation years.
Ignores the lrev flags (`--sl-*`, `--tf`, `--flow-*`, `--order-age`).

| Key | Meaning | Default (lowdd) |
|---|---|---|
| `z_entry` | min normalized daily move to call it a pullback | 0.5 (0.6) |
| `z_cap` | max — beyond this it's a blow-off, no trade | 4.0 |
| `trend_strength_min` | regime gate: MA-slope/ATR must exceed this | 0.5 |
| `trend_win` | sessions in the trend MA | 50 |
| `slope_lag` | sessions for the MA slope lookback | 10 |
| `z_win` | sessions for ATR and \|ret\| normalization | 20 |
| `stop_atr` | SL = this × ATR | 1.0 |
| `target_atr` | TP = this × ATR (R:R = target/stop) | 1.5 |
| `max_hold` | time stop after N held sessions | 10 |
| `max_concurrent` | simultaneous positions | 2 (3) |
| `qty` | size per entry in backtest | 0.5 (1/3) |
| `allow_long` / `allow_short` | per-side kill switches | true |
| `min_session_ticks` | drop illiquid/partial sessions from signals | 200 |

```
python run/backtest.py --engine gtrend --set allow_short=false   # long-only research
```

---

## 6. MT5 checks (before live trading) — `scripts/test_mt5.py`

| Option | Meaning | Default |
|---|---|---|
| `--symbol XAUUSD+` | MT5 symbol to check (connection, account, algo-trading setting, live spread) | XAUUSD |
| `--lots 0.01` | size used by the test order | 0.01 |
| `--place-test-order` | full order round-trip: opens + closes a test position (DEMO accounts only — refuses on real) | off |

```
python scripts/test_mt5.py --symbol XAUUSD+
python scripts/test_mt5.py --symbol XAUUSD+ --place-test-order
```

MT5 terminal must be **running and logged in**, with Tools → Options →
Expert Advisors → "Allow algorithmic trading" enabled.

---

## 7. Live trading — `run/live.py`

### All live options

| Option | Meaning | Default |
|---|---|---|
| `--broker` | `paper` (simulated fills, always safe) or `mt5` (real orders) | paper |
| `--symbols GC` (alias `--symbol`) | what to trade; comma list runs MULTI-SYMBOL/MULTI-ENGINE in one terminal. Per-entry format: `NAME[:MT5SYMBOL[:LOTS[:ENGINE]]]` | GC |
| `--engine` | `lrev` / `gtrend` / `gtrend-lowdd` | lrev |
| `--mt5-symbol XAUUSD+` | MT5 symbol override | registry (GC→XAUUSD) |
| `--lots 0.01` | lots per entry (per-symbol override via `--symbols`) | 0.01 |
| `--no-flow-gate` | run the v2-ea configuration live (lrev) | off |
| `--cost 0.4` | paper-mode commission+slippage per round turn (points) | 0.4 |
| `--trades-csv F` | paper trade log path | `logs\paper_trades_<SYM>.csv` |
| `--state-json F` | state snapshot path (written on exit) | `logs\state_<SYM>.json` |
| *strategy flags* | all of section 4 (`--rr`, `--sl-*`, `--set`, ...) | — |

Examples:

```
python run/live.py                                            # GC paper (always safe)
python run/live.py --broker mt5 --mt5-symbol XAUUSD+ --lots 0.01     # real orders
python run/live.py --symbol SI --broker mt5 --mt5-symbol XAGUSD --lots 0.01
python run/live.py --broker mt5 --symbols GC:XAUUSD+:0.01,SI:XAGUSD+:0.02   # multi-symbol
python run/live.py --broker mt5 --symbols GC:XAUUSD+:0.01:lrev,GC:XAUUSD+:0.01:gtrend
                                                              # multi-ENGINE, one terminal
python run/live.py --no-flow-gate                             # v2-ea config live
```

Multi-symbol/engine: leave a spec field empty to use the default
(`core/symbols.py` for the MT5 symbol, `--lots`, `--engine`). Every other
flag applies to ALL entries. Each entry runs as its own child process:
lines are prefixed `[GC]` / `[GC/gtrend]`, a child that dies restarts
automatically after 10s, one child's crash never stops the others, Ctrl-C
stops all. Output files are engine-aware so parallel engines never
overwrite each other: lrev keeps the plain names (`mt5_signals_GC.csv`,
`state_GC.json`), every other engine gets a suffix
(`mt5_signals_GC_gtrend.csv`, `state_GC_gtrend.json`, `live_GC_gtrend_*.log`).
The same holds if you simply open two terminals with different `--engine`.

WARNING: strategy settings were validated on GC ONLY. For any other symbol,
backtest thoroughly (download → prep → backtest, ideally multiple years)
before paper trading it, and paper trade before real money. Also verify your
broker's CFD lot size equals the futures contract size — GC/XAUUSD match
1:1, but e.g. oil CFDs are often 1/10th of a CL contract.

While running you'll see: a `[heartbeat]` line every minute (tick count,
quote, engine status — proof data is flowing), signal/level lines, gate
`SKIPPED` lines, and order fills. Stop with **Ctrl-C** — open MT5 positions
keep their server-side SL/TP.

Files written (all in `logs\`): `paper_trades_<SYMBOL>.csv` (paper mode
trade log), `state_<SYMBOL>.json` (state snapshot on exit),
`mt5_signals_<SYMBOL>.csv` (every MT5 order attempt, GC side + MT5 side),
`live_<SYMBOL>_<start time>.log` (full copy of everything the session
printed — one file per run, survives after the terminal is closed).

If the data stream drops (network outage, PC slept), live.py reconnects on
its own: it saves state, retries after 5s → 15s → 60s → 5 min, and keeps
trying until the stream is back. You lose signals only while the connection
is actually down.

Good to know: lrev makes ~3–5 signals/day, gtrend ~3/MONTH — long quiet
stretches are normal. GC halts daily ~2:30–3:30 AM IST and weekends
Fri ~2:30 AM → Sun ~3:30 AM IST. Disable Windows sleep.

---

## 8. Data watcher — `run/watch.py`

Live read-only stream viewer for any symbol + any Databento schema, with a
session-anchored **cumulative delta (CVD)** — buy volume minus sell volume,
reset at the CME 17:00-ET boundary. No orders, no strategy: safe to run in
its own terminal alongside live trading.

| Option | Meaning | Default |
|---|---|---|
| `--symbol GC` | symbol from `core/symbols.py` | GC |
| `--schema` | `tbbo` (trade + best bid/ask) / `trades` / `mbp-1` (top of book) / `mbp-10` (10-level depth) / `mbo` (every order event) | tbbo |
| `--min-size 10` | print only trades of at least this many contracts (CVD still counts everything) | 0 |
| `--quiet` | no tape — only per-minute summaries and BIG alerts | off |
| `--book-secs 1.0` | min seconds between book snapshots (mbp schemas) | 1.0 |
| `--big-mult 10` | BIG alert when a print/sweep ≥ this × the rolling average trade size | 10 |
| `--big-min 20` | absolute floor for a BIG alert (contracts) | 20 |
| `--sweep-ms 50` | same-side fills within this many ms cluster into ONE sweep and are judged by their sum | 50 |

```
python run/watch.py                                   # GC TBBO tape + CVD
python run/watch.py --symbol SI --schema trades
python run/watch.py --schema mbp-10                   # depth + imbalance watch
python run/watch.py --schema mbo --quiet              # add/cancel counters per minute
python run/watch.py --min-size 10                     # big prints only
```

What you see: every trade (`12:34:56.789  BUY  12 @ 4052.30  CVD +1,234
[4052.2/4052.4]`), **BIG-trade alerts** — single large prints AND sweeps
(one aggressor's burst of same-side fills within `--sweep-ms`, judged by
their SUM):

```
>>> BIG SELL sweep 142 @ 4052.10->4051.60 in 38ms (n=14, 31x avg)  CVD -890
>>> BIG BUY  print 45 @ 4051.00 (n=1, 19x avg)  CVD +45
```

plus a `[1m]` summary each minute (OHLC, volume, minute delta, CVD, trade
count, big-trade counters `big 3B/1S +180` — and add/cancel/modify counters
on mbo), book snapshots on the mbp schemas (mbp-10 shows total bid vs ask
depth and the imbalance %), and a session total on Ctrl-C including big-buy
vs big-sell volume. Everything shown is also written to
`logs\watch_<SYMBOL>_<schema>_<start time>.log` (one file per run, printed
as the first line), so tape history and BIG alerts survive after the
terminal closes. Auto-reconnects if the stream drops. Schema
availability depends on your Databento license; note that on `mbo`, only
trade events feed the CVD (a resting ask-side add is a quote, not a sale).

---

## 9. Where things live

| Path | What it is |
|---|---|
| `engines\lrev.py` | THE validated strategy (single source of truth, backtest + live) |
| `engines\gtrend.py` | G-Trend: daily trend-pullback engine (spec: `docs\GTREND_SPEC.md`; GC only — SI tested negative) |
| `engines\__init__.py` | engine registry - add new strategies here |
| `core\` | shared machinery: brokers, data/replay, reports, CLI flags, symbols, paths |
| `run\backtest.py` / `run\live.py` | the two runners (same engines, same flags) |
| `run\watch.py` | live data watcher: any schema, tape + cumulative delta (read-only) |
| `scripts\` | data prep, data download, MT5 connection test |
| `logs\` | ALL generated output: trade CSVs, MT5 signal logs, state files (gitignored) |
| `config.py` | your secrets (gitignored) |
| `Data\` / `data_cache\` | raw DBN files / parquet cache (both gitignored) |
| `docs\TBBO_Research_Report.md` | how the strategy was found and validated |
| `archive\` | original EAs, study code and study trade logs (not for trading) |

---

## 10. A warning that belongs in every command file

When you sweep `--rr` / `--sl-*` / `--set` values, tune on one period
(e.g. 2023–2024) and confirm on a period the tuning never saw (2025–2026).
A setting that only wins in one exact combination is noise. And remember
the 2023–2026 result: L-Rev only earned in high-volatility regimes —
paper/demo first, small size always.
