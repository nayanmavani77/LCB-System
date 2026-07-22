# LCB-System

Gold futures (GC) trading system: **L-Rev v2**, a swing-level breakout strategy
derived from the original L/CB EA through TBBO (tick + order-flow) research.
Full methodology and findings: [`docs/TBBO_Research_Report.md`](docs/TBBO_Research_Report.md).

**One engine, one source of truth:** the strategy exists exactly once, in
`lrev/strategy.py`. The backtest replays historical ticks through it; live
trading streams real-time ticks through it. There is no separate backtest
implementation, so backtest behaviour == live behaviour by construction.

## Layout

    lrev/            THE strategy engine (single source of truth)
      strategy.py      L-Rev v2 rules: levels, gates, triggers, exits
      broker.py        Broker interface + PaperBroker simulator
      data.py          cache loading + tick replay
    backtest.py      backtest runner (tick replay through lrev/)
    live.py          live runner (real-time Databento TBBO through lrev/)
    scripts/         prep.py (DBN -> cache), download_data.py (extend Data/)
    results/         trade logs from the original research study
    docs/            research report + equity curve
    research/        ARCHIVE: vectorized study code that produced the report
                     numbers - kept for reproducibility, not for trading
    ea/              ARCHIVE: MT5 versions (v1 original + v2 port), unused
    Data/            raw Databento DBN files (gitignored)
    data_cache/      parquet cache built by scripts/prep.py (gitignored)

## Quick start

    pip install databento pandas pyarrow zstandard numpy
    python3 scripts/prep.py                                   # build cache (once per dataset)
    python3 backtest.py --start 2026-04-01 --end 2026-07-17   # any date window
    python3 backtest.py --config v2-ea --csv trades.csv       # export trades
    python3 live.py                                           # real-time paper trading

Backtest configs: `v2-flow` (default: age cap 35h + spread cap $0.90 + flow
gate), `v2-ea` (no flow gate), `v1` (original, gates off). New data: drop any
number of `*tbbo*`/`*ohlcv1m*` DBN files into `Data/` (per-year files are
fine) and re-run `scripts/prep.py` - contract windows are read from every
file's metadata, merged across year boundaries, and the backtest date range
extends automatically. Multi-year prep decodes one TBBO file at a time
(peak RAM roughly one year, ~2-3 GB).

To trade a real account, implement the small `Broker` interface in
`lrev/broker.py` for your venue (e.g. IBKR via ib_insync) and pass it to
`LRevStrategy` in place of `PaperBroker`. The strategy code does not change.

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
