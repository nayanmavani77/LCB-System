# LCB-System

Gold futures (GC) trading system research: the original L/CB EA, and the
TBBO-powered **L-Rev v2** strategy derived from it. Full methodology and
results: [`docs/TBBO_Research_Report.md`](docs/TBBO_Research_Report.md).

## Layout

    live/       event-driven Python engine: replay / live paper trading / broker interface
    ea/         MT5 expert advisors (reference; not needed for the Python workflow)
                  LCB-System_v1.mq5          original combined EA (reference)
                  L-Rev-System_TBBO_v2.mq5   v2 strategy (use this one)
                  tbbo_flow_bridge.py        live order-flow bridge for the optional flow gate
    backtest/   Python backtest engine (tick-accurate fills on TBBO data)
    results/    trade logs of the final configurations
    docs/       research report + equity curve
    Data/       raw Databento DBN files (gitignored - too large for git)
    data_cache/ parquet cache built by prep.py (gitignored)

## Reproduce the backtest

    pip install databento pandas pyarrow zstandard numpy
    python3 backtest/prep.py          # decode DBN -> data_cache/ (run once per new dataset)
    python3 backtest/run_backtest.py --start 2026-04-01 --end 2026-07-17 --config v2-flow

Configs: `v1-l`, `v1-cb`, `v2-ea`, `v2-flow`. Dates outside the available data
are clamped automatically. Add `--csv trades.csv` to export the trade list.

New data: drop updated `*ohlcv1m*.dbn.zst` and `*tbbo*.dbn.zst` files into
`Data/` and re-run `prep.py` - the contract windows are read from the DBN
metadata, so the backtest window extends automatically.

## Run it live (paper trading) - no MetaTrader needed

The same strategy exists as an event-driven Python engine in `live/`:

    python3 live/run_live.py --mode replay --start 2026-06-01 --end 2026-07-17
    python3 live/run_live.py --mode live          # real-time Databento TBBO, paper fills

`--mode live` needs a live-enabled DATABENTO_API_KEY: it bootstraps warmup
bars from historical, streams real-time TBBO for GC.v.0, applies all v2
gates (age cap, spread cap, flow gate) and simulates fills with PaperBroker,
logging trades to `paper_trades.csv`. To trade a real account later,
implement the small `Broker` interface in `live/broker.py` for your venue
(e.g. IBKR via ib_insync) - the strategy code does not change.
`live/download_data.py` extends `Data/` with fresh history for backtests.

Replay validation: the event engine reproduces the vectorized backtest on the
out-of-sample window (134 trades, +165 pts, PF 1.16 vs 150 / +189 / PF 1.16;
the small gap is warmup handling at the contract-roll boundary).

## Headline results (Dec 2025 - Jul 2026, 1 contract, net of costs)

| Config | Trades | P&L (pts) | PF | OOS (GCQ6) |
|---|---|---|---|---|
| v1 CB-System | 1,266 | -3,094 | 0.79 | - |
| v1 L-System | 1,150 | -424 | 0.97 | - |
| v2 EA-only | 694 | +1,122 | 1.16 | +127 pts |
| v2 + flow gate | 546 | +1,625 | 1.31 | +189 pts |

Not financial advice; see the report's caveats section.
