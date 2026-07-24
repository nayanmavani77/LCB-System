# LCB-System

Futures trading system (GC-validated, multi-symbol capable): **L-Rev v2**, a swing-level breakout strategy
derived from the original L/CB EA through TBBO (tick + order-flow) research.
Full methodology and findings: [`docs/TBBO_Research_Report.md`](docs/TBBO_Research_Report.md).

**One engine, one source of truth:** the strategy exists exactly once, in
`engines/lrev.py`. The backtest replays historical ticks through it; live
trading streams real-time ticks through it. There is no separate backtest
implementation, so backtest behaviour == live behaviour by construction.

## Layout

    run/             the two RUNNERS (same engines, same flags)
      backtest.py      replays history through an engine
      live.py          streams real-time ticks through an engine (paper or MT5)
    engines/         STRATEGIES - one file per engine, add new ones here
      lrev.py          L-Rev: level-BREAK engine (validated on GC, OOS on SI)
      ldef.py          L-Def: level-DEFEND engine (experimental, tested negative)
    core/            shared machinery (engine-agnostic)
      broker.py        Broker interface + PaperBroker simulator
      mt5_broker.py    MT5 execution adapter (futures signals -> CFD orders)
      data.py          cache loading + tick replay
      report.py        terminal metrics report + portfolio section
      cli.py           shared strategy flags (same in backtest & live)
      symbols.py       symbol registry (GC/SI/HG/PL/CL/NG - add more here)
      paths.py         routes all generated files into logs/
    scripts/         prep.py (DBN -> cache), download_data.py, test_mt5.py
    logs/            ALL generated output: trade CSVs, MT5 signal logs,
                     state snapshots (created automatically, gitignored)
    docs/            COMMANDS.md (all commands) + research report + equity curve
    archive/         history: original EAs, study code, study trade logs
    Data/            raw Databento DBN files (gitignored)
    data_cache/      parquet cache built by scripts/prep.py (gitignored)
    config.py        your secrets - template in docs/COMMANDS.md (gitignored)

Adding a new strategy = one file in engines/ + one line in engines/__init__.py;
it immediately works with every symbol, backtest, live, MT5 and the reports.

**All commands: [`docs/COMMANDS.md`](docs/COMMANDS.md)**

## Quick start

    pip install databento pandas pyarrow zstandard numpy
    python scripts/prep.py                                       # build cache (once per dataset)
    python run/backtest.py --start 2026-04-01 --end 2026-07-17   # any date window
    python run/backtest.py --config v2-ea --csv trades.csv      # export trades (-> logs/)
    python run/live.py                                           # real-time paper trading

Backtest configs: `v2-flow` (default: age cap 35h + spread cap $0.90 + flow
gate), `v2-ea` (no flow gate), `v1` (original, gates off). New data: drop any
number of `*tbbo*`/`*ohlcv1m*` DBN files into `Data/` (per-year files are
fine) and re-run `scripts/prep.py` - contract windows are read from every
file's metadata, merged across year boundaries, and the backtest date range
extends automatically. Multi-year prep decodes one TBBO file at a time
(peak RAM roughly one year, ~2-3 GB).

Live MT5 execution is built in (`core/mt5_broker.py`, futures signal ->
CFD order with SL/TP as re-anchored distances). For any other venue,
implement the small `Broker` interface in `core/broker.py` and pass it to
the strategy in place of `PaperBroker`. The strategy code does not change.

## Results - unified engine, Dec 28 2025 - Jul 17 2026

Net of quoted spread at fill + 0.15 pts/round-turn costs, 1 contract, GC = $100/pt.
Rules were selected on Dec-May data only; May 29 - Jul 17 was a held-out
validation window (see the report).

| Config | Trades | P&L (pts) | PF | Max DD | Held-out window |
|---|---|---|---|---|---|
| v1 (original L-System) | ~1,150 | -424 | 0.97 | 1,433 | - |
| v2-ea | 688 | +1,059 | 1.15 | 502 | +110 pts / PF 1.07 |
| v2-flow | 545 | +1,457 | 1.27 | 386 | +134 pts / PF 1.12 |

(The research report's tables show the original study numbers from the
archived vectorized code; small differences vs the unified engine come from
bar construction details. The unified engine is authoritative going forward.)

Not financial advice. Six months of a trending gold market is a regime, not
a lifetime - re-validate as data accrues and size conservatively.
