"""MT5 connection test - run this BEFORE live trading to verify everything.

    python scripts/test_mt5.py --symbol XAUUSD
    python scripts/test_mt5.py --symbol XAUUSD --place-test-order

Checks: package installed, terminal reachable, account logged in, symbol
exists and is tradeable. With --place-test-order it opens a 0.01-lot BUY
with a wide SL/TP and closes it seconds later (costs ~1 spread on DEMO;
do NOT run it on a real account).
"""
import argparse
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--lots", type=float, default=0.01)
    ap.add_argument("--place-test-order", action="store_true")
    args = ap.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise SystemExit("MetaTrader5 package missing: pip install MetaTrader5")

    if not mt5.initialize():
        raise SystemExit(f"Cannot reach MT5 terminal: {mt5.last_error()}\n"
                         "Is the MT5 terminal running and logged in?")
    acct = mt5.account_info()
    print(f"OK  connected: account {acct.login} on {acct.server}")
    print(f"    balance {acct.balance} {acct.currency}, "
          f"trade_mode={'DEMO' if acct.trade_mode == 0 else 'REAL/CONTEST'}")
    if not mt5.terminal_info().trade_allowed:
        print("WARNING: Algo trading is DISABLED in the terminal "
              "(Tools > Options > Expert Advisors > Allow algorithmic trading)")

    info = mt5.symbol_info(args.symbol)
    if info is None:
        syms = [s.name for s in mt5.symbols_get() if "XAU" in s.name.upper()
                or "GOLD" in s.name.upper()]
        raise SystemExit(f"symbol {args.symbol} not found. "
                         f"Gold-like symbols on this broker: {syms}")
    mt5.symbol_select(args.symbol, True)
    tick = mt5.symbol_info_tick(args.symbol)
    print(f"OK  {args.symbol}: bid {tick.bid} ask {tick.ask} "
          f"spread {(tick.ask - tick.bid):.2f}")

    if not args.place_test_order:
        print("\nAll checks passed. Add --place-test-order (DEMO only!) to "
              "test a full order round-trip.")
        mt5.shutdown()
        return

    if acct.trade_mode != 0:
        raise SystemExit("refusing --place-test-order: this is NOT a demo account")

    px = tick.ask
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": args.symbol,
           "volume": args.lots, "type": mt5.ORDER_TYPE_BUY, "price": px,
           "sl": round(px - 50, info.digits), "tp": round(px + 50, info.digits),
           "deviation": 30, "magic": 26031604, "comment": "L-Rev|TEST",
           "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
    r = mt5.order_send(req)
    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        req["type_filling"] = mt5.ORDER_FILLING_FOK
        r = mt5.order_send(req)
    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        raise SystemExit(f"test order FAILED: {r.retcode if r else None} "
                         f"{r.comment if r else mt5.last_error()}")
    print(f"OK  test BUY filled @ {r.price} (ticket {r.order})")
    time.sleep(3)

    tick = mt5.symbol_info_tick(args.symbol)
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": args.symbol,
           "volume": args.lots, "type": mt5.ORDER_TYPE_SELL, "price": tick.bid,
           "position": r.order, "deviation": 30, "magic": 26031604,
           "comment": "L-Rev|TEST-CLOSE", "type_time": mt5.ORDER_TIME_GTC,
           "type_filling": mt5.ORDER_FILLING_IOC}
    c = mt5.order_send(req)
    if c is None or c.retcode != mt5.TRADE_RETCODE_DONE:
        req["type_filling"] = mt5.ORDER_FILLING_FOK
        c = mt5.order_send(req)
    print(f"OK  test position closed @ {c.price if c else '?'}"
          if c and c.retcode == mt5.TRADE_RETCODE_DONE
          else f"close it manually in MT5 (ticket {r.order})")
    print("\nMT5 round-trip works. You are ready for: "
          "python live.py --broker mt5 --mt5-symbol " + args.symbol)
    mt5.shutdown()


if __name__ == "__main__":
    main()
