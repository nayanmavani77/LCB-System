"""Shared CLI options for strategy parameters - used by backtest.py AND live.py
so the exact same flags configure both (single source of truth, including config).
"""
from __future__ import annotations


def add_strategy_args(ap):
    g = ap.add_argument_group("strategy parameters (same flags in backtest & live)")
    g.add_argument("--rr", type=float, default=None,
                   help="take-profit = SL distance x RR (default 2.0)")
    g.add_argument("--sl-m15", type=float, default=None,
                   help="M15 SL multiplier x median true range (default 1.5)")
    g.add_argument("--sl-h1", type=float, default=None,
                   help="H1 SL multiplier (default 0.5)")
    g.add_argument("--sl-h4", type=float, default=None,
                   help="H4 SL multiplier (default 0.5)")
    g.add_argument("--tf", default=None,
                   help="comma list of timeframes to trade, e.g. M15,H1 "
                        "(default M15,H1,H4)")
    g.add_argument("--max-spread", type=float, default=None,
                   help="spread gate in $ (default 0.90; 0 disables)")
    g.add_argument("--order-age", type=float, default=None,
                   help="cancel unfilled level after N hours (default 35; 0 disables)")
    g.add_argument("--flow-lo", type=float, default=None,
                   help="flow gate lower bound (default 0.0)")
    g.add_argument("--flow-hi", type=float, default=None,
                   help="flow gate upper bound (default 0.6)")
    g.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override ANY engine config key, repeatable - e.g. "
                        "--set delta_threshold=0.7 --set require_color=true")


def config_from_args(args, base: dict | None = None) -> dict:
    """Merge CLI overrides into a strategy config dict."""
    from engines.lrev import DEFAULT_CONFIG

    cfg = dict(DEFAULT_CONFIG)
    cfg["timeframes"] = dict(DEFAULT_CONFIG["timeframes"])
    if base:
        for k, v in base.items():
            if k == "timeframes":
                cfg["timeframes"] = dict(v)
            else:
                cfg[k] = v

    if getattr(args, "rr", None) is not None:
        cfg["rr"] = args.rr
    for tf, arg in (("M15", "sl_m15"), ("H1", "sl_h1"), ("H4", "sl_h4")):
        v = getattr(args, arg, None)
        if v is not None:
            cfg["timeframes"][tf] = v
    if getattr(args, "tf", None):
        wanted = {t.strip().upper() for t in args.tf.split(",") if t.strip()}
        unknown = wanted - set(cfg["timeframes"])
        if unknown:
            raise SystemExit(f"unknown timeframe(s): {sorted(unknown)} "
                             f"(valid: {sorted(cfg['timeframes'])})")
        cfg["timeframes"] = {k: v for k, v in cfg["timeframes"].items()
                             if k in wanted}
    if getattr(args, "max_spread", None) is not None:
        cfg["max_spread"] = args.max_spread
    if getattr(args, "order_age", None) is not None:
        cfg["order_max_age_h"] = args.order_age
    if getattr(args, "flow_lo", None) is not None:
        cfg["flow_lo"] = args.flow_lo
    if getattr(args, "flow_hi", None) is not None:
        cfg["flow_hi"] = args.flow_hi
    for kv in getattr(args, "set", None) or []:
        key, sep, raw = kv.partition("=")
        key, raw = key.strip(), raw.strip()
        if not sep or not key:
            raise SystemExit(f"--set expects KEY=VALUE, got '{kv}'")
        try:
            val = int(raw)
        except ValueError:
            try:
                val = float(raw)
            except ValueError:
                val = {"true": True, "false": False}.get(raw.lower(), raw)
        cfg[key] = val
    return cfg


def describe(cfg: dict) -> str:
    tfs = ", ".join(f"{tf} SLx{m}" for tf, m in cfg["timeframes"].items())
    eng = cfg.get("engine_name", "L-Rev")
    fg = "" if cfg["use_flow_gate"] else " (flow gate OFF)"
    return (f"{eng} | RR {cfg['rr']} | {tfs} | spread<= {cfg['max_spread']} | "
            f"age<= {cfg['order_max_age_h']}h | "
            f"flow [{cfg['flow_lo']},{cfg['flow_hi']}]{fg}")
