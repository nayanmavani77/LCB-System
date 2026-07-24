"""Terminal performance report - printed right after every backtest/live session."""
from __future__ import annotations

import pandas as pd


def print_report(broker, title="BACKTEST RESULT", point_value=100.0):
    """Full metrics table from a PaperBroker's closed trades."""
    closed = broker.closed
    line = "=" * 58
    print(f"\n{line}\n  {title}\n{line}")
    if not closed:
        print("  no trades")
        return

    df = pd.DataFrame(closed)
    df["dt"] = pd.to_datetime(df["ts_open"], utc=True)
    df = df.sort_values("ts_open").reset_index(drop=True)
    # size-weight the points so fractional-lot engines (e.g. G-Trend's
    # 2 x 0.5) report P&L on the SIZED basis; qty=1 engines are unchanged
    if "qty" in df.columns:
        q = df["qty"].fillna(1.0)
        df["pnl_pts"] = df["pnl_pts"] * q
        if "pnl_gross_pts" in df.columns:
            df["pnl_gross_pts"] = df["pnl_gross_pts"] * q
    pts = df["pnl_pts"]

    wins = pts[pts > 0]
    losses = pts[pts < 0]
    eq = pts.cumsum()
    dd = (eq.cummax() - eq).max()
    pf = wins.sum() / -losses.sum() if len(losses) and losses.sum() < 0 else float("inf")
    risk = (df["entry"] - df["sl"]).abs()
    if "qty" in df.columns:
        risk = risk * df["qty"].fillna(1.0)   # pts are size-weighted above
    r = (pts / risk.replace(0, float("nan"))).dropna()

    days = max(1e-9, (df.dt.iloc[-1] - df.dt.iloc[0]).total_seconds() / 86400)
    cost_pts = getattr(broker, "cost_pts", 0.0)
    if "pnl_gross_pts" in df.columns:
        gross = df["pnl_gross_pts"]
    else:
        gross = pts + cost_pts
    total_cost = gross.sum() - pts.sum()
    print(f"  period          : {df.dt.iloc[0].date()} .. {df.dt.iloc[-1].date()}  ({days:.0f} days)")
    print(f"  trades          : {len(df)}   ({len(df)/max(days/30.4,1e-9):.0f}/month)")
    print(f"  gross P&L       : {gross.sum():+,.1f} pts   (${gross.sum()*point_value:+,.0f})   [before costs]")
    print(f"  costs           : -{total_cost:,.1f} pts   (${total_cost*point_value:,.0f})   "
          f"[{len(df)} trades x {cost_pts} pts/round turn: commission+slippage;"
          f" quoted spread additionally paid inside fill prices]")
    print(f"  net P&L         : {pts.sum():+,.1f} pts   (${pts.sum()*point_value:+,.0f} per contract)")
    print(f"  avg / trade     : {pts.mean():+.2f} pts   ({r.mean():+.3f} R)")
    print(f"  win rate        : {100*(pts>0).mean():.1f}%")
    print(f"  profit factor   : {pf:.2f}")
    print(f"  avg win / loss  : {wins.mean() if len(wins) else 0:+.2f} / "
          f"{losses.mean() if len(losses) else 0:+.2f} pts")
    print(f"  best / worst    : {pts.max():+.1f} / {pts.min():+.1f} pts")
    print(f"  max drawdown    : {dd:,.1f} pts   (${dd*point_value:,.0f})")
    print(f"  open positions  : {len(broker.positions)}")

    dt_naive = df.dt.dt.tz_convert(None)

    def block(name, key):
        g = df.groupby(key)["pnl_pts"]
        t = pd.DataFrame({
            "trades": g.size(),
            "pts": g.sum().round(1),
            "usd": (g.sum() * point_value).round(0).astype(int),
            "win%": (100 * g.apply(lambda x: (x > 0).mean())).round(1),
            "avg": g.mean().round(2),
        })
        print(f"\n  -- {name} " + "-" * max(1, 44 - len(name)))
        print("  " + t.to_string().replace("\n", "\n  "))

    if dt_naive.dt.year.nunique() > 1:
        df["year"] = dt_naive.dt.year
        block("by year", "year")

    df["month"] = dt_naive.dt.to_period("M")
    g = df.groupby("month")["pnl_pts"]
    t = pd.DataFrame({
        "trades": g.size(),
        "pts": g.sum().round(1),
        "cum_pts": g.sum().cumsum().round(1),
        "win%": (100 * g.apply(lambda x: (x > 0).mean())).round(1),
    })
    print("\n  -- by month " + "-" * 36)
    print("  " + t.to_string().replace("\n", "\n  "))

    if "tag" in df.columns:
        tf = df["tag"].astype(str).str.split("|").str[1]
        if tf.notna().any():
            df["timeframe"] = tf
            block("by timeframe", "timeframe")
    df["direction"] = df["dir"].map({1: "long", -1: "short"})
    block("by direction", "direction")
    block("by exit reason", "reason")
    print(line)


def print_portfolio(dfs):
    """Combined parallel-account section. dfs = list of (symbol, trades_df)
    where trades_df rows carry pnl_usd (already scaled by each symbol's
    point value). Prints only the cross-symbol view - per-symbol detail is
    print_report's job."""
    frames = []
    rows = []
    for symbol, df in dfs:
        if df is None or df.empty:
            continue
        d = df.copy()
        d["symbol"] = symbol
        frames.append(d)
        usd = d["pnl_usd"]
        pf = usd[usd > 0].sum() / max(1e-9, -usd[usd < 0].sum())
        rows.append({"symbol": symbol, "trades": len(d),
                     "net_usd": round(usd.sum(), 0),
                     "avg_usd": round(usd.mean(), 1),
                     "win%": round(100 * (usd > 0).mean(), 1),
                     "pf": round(pf, 2)})
    if len(frames) < 2:
        return
    line = "=" * 58
    print(f"\n{line}\n  PORTFOLIO RESULT (symbols combined, in $)\n{line}")
    print("  " + pd.DataFrame(rows).to_string(index=False).replace("\n", "\n  "))
    port = (pd.concat(frames, ignore_index=True)
            .sort_values("ts_close").reset_index(drop=True))
    usd = port["pnl_usd"]
    eq = usd.cumsum()
    dd = (eq.cummax() - eq).max()
    pf = usd[usd > 0].sum() / max(1e-9, -usd[usd < 0].sum())
    wins, losses = usd[usd > 0], usd[usd < 0]
    print(f"\n  combined trades : {len(port)}")
    print(f"  combined net    : ${usd.sum():+,.0f}")
    print(f"  avg / trade     : ${usd.mean():+,.1f}")
    print(f"  win rate        : {100 * (usd > 0).mean():.1f}%")
    print(f"  combined PF     : {pf:.2f}")
    print(f"  avg win / loss  : ${wins.mean() if len(wins) else 0:+,.0f} / "
          f"${losses.mean() if len(losses) else 0:+,.0f}")
    print(f"  combined max DD : ${dd:,.0f}   <- joint drawdown of running "
          f"these symbols in parallel")
    port["month"] = (pd.to_datetime(port["ts_close"], utc=True)
                     .dt.tz_convert(None).dt.to_period("M"))
    neg = (port.groupby(["month", "symbol"])["pnl_usd"].sum() < 0)
    joint = neg.groupby("month").sum()
    if len(joint) and joint.max() > 1:
        print(f"  correlation note: worst month {joint.idxmax()} had "
              f"{joint.max()} symbols negative simultaneously")
    m = port.groupby("month")["pnl_usd"].agg(n="size", usd="sum").round(0)
    m["cum_usd"] = m["usd"].cumsum()
    print("\n  -- combined monthly ($) " + "-" * 24)
    print("  " + m.to_string().replace("\n", "\n  "))
    print(line)
