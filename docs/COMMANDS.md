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

## 2. Data (multi-symbol)

| Command | What it does |
|---|---|
| `python scripts/prep.py` | build the GC cache from DBN files in `Data\` — run once, and again whenever you add data |
| `python scripts/prep.py --symbol SI` | build another symbol's cache from `Data\SI\` |
| `python scripts/download_data.py --symbol GC --start 2026-07-18 --end 2026-09-01` | download data from Databento into `Data\<SYMBOL>\` (then re-run prep.py for that symbol) |

Known symbols (add more in `core/symbols.py`, one dict entry each):
GC (gold), SI (silver), HG (copper), PL (platinum), CL (WTI oil), NG (nat gas).
Everything symbol-specific (Databento symbols, $/point, default spread gate,
default cost, default MT5 symbol) lives in that registry — the strategy code
never changes per symbol.

---

## 3. Backtest

Basic form:

```
python run/backtest.py --start YYYY-MM-DD --end YYYY-MM-DD
```

| Option | Meaning | Default |
|---|---|---|
| `--start` / `--end` | date window (clamped to available data) | all data |
| `--config` | `v2-flow` (all gates) / `v2-ea` (no flow gate) / `v1` (no gates) | `v2-flow` |
| `--csv FILE.csv` | save every trade to a CSV | off |
| `--verbose` | print every level, gate decision and fill | off |

Examples:

```
python run/backtest.py                                        # GC, everything, default strategy
python run/backtest.py --start 2026-04-01 --end 2026-07-17
python run/backtest.py --symbols SI --start 2025-01-01        # another symbol (cache required)
python run/backtest.py --symbols GC,SI --start 2025-01-01     # PARALLEL: per-symbol reports
                                                              # + combined portfolio ($, joint max DD)
python run/backtest.py --start 2023-01-01 --end 2026-01-01 --csv trades.csv
python run/backtest.py --config v1                            # original ungated system
python run/backtest.py --engine ldef                          # experimental defend engine
```

ONE command for everything: one symbol -> full detailed report; several
symbols (comma-separated) -> detailed report per symbol plus a combined
portfolio section showing net $, PF and the JOINT max drawdown as if all
symbols ran in parallel in one account. With --csv and multiple symbols,
one file per symbol is written (trades_GC.csv, trades_SI.csv, ...).
`--cost` and `--max-spread` default to per-symbol values from `core/symbols.py`.
All generated CSVs land in `logs\` (give an absolute path to override).

---

## 4. Strategy settings (SAME flags for backtest AND live)

Add these to `run/backtest.py` or `run/live.py` — identical meaning in both:

| Flag | Meaning | Default |
|---|---|---|
| `--rr 3.0` | take-profit = SL distance × RR | 2.0 |
| `--sl-m15 1.0` | M15 stop = multiplier × median true range | 1.5 |
| `--sl-h1 0.5` | H1 stop multiplier | 0.5 |
| `--sl-h4 0.5` | H4 stop multiplier | 0.5 |
| `--tf M15,H1` | trade only these timeframes | M15,H1,H4 |
| `--max-spread 0.9` | skip triggers when spread > $X (0 = off) | 0.90 |
| `--order-age 35` | cancel unfilled level after N hours (0 = off) | 35 |
| `--flow-lo 0.0` `--flow-hi 0.6` | flow-gate band (aligned 30s imbalance) | 0.0–0.6 |

Workflow: validate a setting in backtest, then run live with the *exact same flags*:

```
python run/backtest.py --start 2026-01-01 --rr 3.0 --sl-m15 1.0
python run/live.py --broker mt5 --mt5-symbol XAUUSD+ --lots 0.01 --rr 3.0 --sl-m15 1.0
```

Every run prints its configuration first (`strategy: RR 3.0 | ...`) so you
always know what's being tested or traded.

---

## 5. MT5 checks (before live trading)

| Command | What it does |
|---|---|
| `python scripts/test_mt5.py --symbol XAUUSD+` | verify terminal connection, account, algo-trading setting, symbol, live spread |
| `python scripts/test_mt5.py --symbol XAUUSD+ --place-test-order` | full order round-trip: opens + closes a 0.01-lot test position (DEMO accounts only — refuses on real) |

MT5 terminal must be **running and logged in**, with Tools → Options →
Expert Advisors → "Allow algorithmic trading" enabled.

---

## 6. Live trading

| Command | What it does |
|---|---|
| `python run/live.py` | GC live signals, **paper fills** (no broker, always safe) |
| `python run/live.py --broker mt5 --mt5-symbol XAUUSD+ --lots 0.01` | GC signals, **real orders into MT5** |
| `python run/live.py --symbol SI --broker mt5 --mt5-symbol XAGUSD --lots 0.01` | another symbol end-to-end (VALIDATE IN BACKTEST FIRST) |
| `python run/live.py --broker mt5 --symbols GC:XAUUSD+:0.01,SI:XAGUSD+:0.02` | **MULTI-SYMBOL in ONE terminal** — per-symbol MT5 symbol and lots |
| `python run/live.py --no-flow-gate` | run the v2-ea configuration live |

Multi-symbol format: `--symbols NAME[:MT5SYMBOL[:LOTS]],...` — leave a field
empty to use the default (`core/symbols.py` registry for the MT5 symbol,
`--lots` for lots). Every other flag (`--rr`, `--tf`, `--no-flow-gate`,
...) applies to ALL symbols. Each symbol runs as its own child process:
every line is prefixed `[GC]` / `[SI]`, a symbol that dies is restarted
automatically after 10s, one symbol's crash never stops the others, and
Ctrl-C stops all of them cleanly. Logs stay per symbol as usual.

MT5 signal logs are per symbol: `logs\mt5_signals_GC.csv`, `logs\mt5_signals_SI.csv`, ...
WARNING: the strategy settings were validated on GC ONLY. For any other
symbol, backtest thoroughly (download data -> prep -> backtest, ideally
multiple years) before paper trading it, and paper trade before real money.
Also verify your broker's CFD lot size equals the futures contract size —
GC/XAUUSD match 1:1, but e.g. oil CFDs are often 1/10th of a CL contract.

While running you'll see: a `[heartbeat]` line every minute (tick count,
GC quote, flow, armed levels — proof data is flowing), `[M15] level ...`
lines when swings are detected, gate `SKIPPED` lines, and order fills.
Stop with **Ctrl-C** — open MT5 positions keep their server-side SL/TP.

Files written (all in `logs\`): `paper_trades_<SYMBOL>.csv` (paper mode
trade log), `state_<SYMBOL>.json` (state snapshot on exit),
`mt5_signals_<SYMBOL>.csv` (every MT5 order attempt, GC side + MT5 side),
`live_<SYMBOL>_<start time>.log` (full copy of everything the session
printed — one file per run, survives after the terminal is closed).

If the data stream drops (network outage, PC slept), live.py now
reconnects on its own: it saves state, retries after 5s → 15s → 60s →
5 min, and keeps trying until the stream is back. You lose signals only
while the connection is actually down.

Good to know: ~3–5 signals/day with long quiet stretches is normal.
GC halts daily ~2:30–3:30 AM IST and weekends Fri ~2:30 AM → Sun ~3:30 AM IST.
Disable Windows sleep. If the stream dies, just run the command again.

---

## 7. Where things live

| Path | What it is |
|---|---|
| `engines\lrev.py` | THE validated strategy (single source of truth, backtest + live) |
| `engines\ldef.py` | experimental defend engine (tested negative - do not trade) |
| `engines\__init__.py` | engine registry - add new strategies here |
| `core\` | shared machinery: brokers, data/replay, reports, CLI flags, symbols, paths |
| `run\backtest.py` / `run\live.py` | the two runners (same engines, same flags) |
| `scripts\` | data prep, data download, MT5 connection test |
| `logs\` | ALL generated output: trade CSVs, MT5 signal logs, state files (gitignored) |
| `config.py` | your secrets (gitignored) |
| `Data\` / `data_cache\` | raw DBN files / parquet cache (both gitignored) |
| `docs\TBBO_Research_Report.md` | how the strategy was found and validated |
| `archive\` | original EAs, study code and study trade logs (not for trading) |

---

## 8. A warning that belongs in every command file

When you sweep `--rr` / `--sl-*` values, tune on one period (e.g. 2023–2024)
and confirm on a period the tuning never saw (2025–2026). A setting that only
wins in one exact combination is noise. And remember the 2023–2026 result:
this strategy only earned in high-volatility regimes — paper/demo first,
small size always.
