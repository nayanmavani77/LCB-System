"""MT5 connection test - run this BEFORE live trading to verify everything.

    python scripts/test_mt5.py --symbol XAUUSD
    python scripts/test_mt5.py --symbol XAUUSD --max-spread 0.9
    python scripts/test_mt5.py --symbol XAUUSD --place-test-order
    python scripts/test_mt5.py --symbol XAUUSD --place-test-order --allow-real

Checks: package installed, terminal reachable, account logged in, symbol
exists and is tradeable. With --place-test-order it opens a 0.01-lot BUY
with a wide SL/TP and closes it seconds later (costs ~1 spread).

On a REAL account --place-test-order refuses unless you also pass
--allow-real, which prints the estimated cost of the round-trip and makes
you type the account number back before any order is sent. Real money:
the test costs roughly one spread + commission on the test lot.
"""
import argparse
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--lots", type=float, default=0.01)
    ap.add_argument("--place-test-order", action="store_true")
    ap.add_argument("--allow-real", action="store_true",
                    help="permit --place-test-order on a REAL account "
                         "(asks you to type the account number to confirm; "
                         "costs ~1 spread + commission on the test lot)")
    ap.add_argument("--max-spread", type=float, default=None,
                    help="spread filter in price units (e.g. 0.9 to mirror "
                         "the GC live gate): the check FAILS and the test "
                         "order is NOT sent while live spread exceeds this")
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
    spread = tick.ask - tick.bid
    print(f"OK  {args.symbol}: bid {tick.bid} ask {tick.ask} "
          f"spread {spread:.2f}")
    if args.max_spread is not None:
        if spread <= args.max_spread:
            print(f"OK  spread filter: {spread:.2f} <= cap {args.max_spread}")
        else:
            print(f"FAIL spread filter: {spread:.2f} > cap {args.max_spread}")

    if not args.place_test_order:
        if args.max_spread is not None and spread > args.max_spread:
            mt5.shutdown()
            raise SystemExit("spread filter FAILED - live spread is wider "
                             "than your cap right now")
        print("\nAll checks passed. Add --place-test-order to test a full "
              "order round-trip (on a REAL account also add --allow-real; "
              "costs ~1 spread + commission).")
        mt5.shutdown()
        return

    if acct.trade_mode != 0:
        if not args.allow_real:
            raise SystemExit(
                "refusing --place-test-order: this is NOT a demo account.\n"
                "To test the order round-trip on this REAL account anyway "
                "(costs ~1 spread + commission on the test lot), add "
                "--allow-real. You will be asked to confirm.")
        # explicit, typed confirmation before touching real money
        contract = getattr(info, "trade_contract_size", 0.0) or 0.0
        est = (tick.ask - tick.bid) * contract * args.lots
        print(f"\n*** REAL ACCOUNT {acct.login} on {acct.server} ***")
        print(f"    test trade: BUY {args.lots} lot {args.symbol}, closed "
              f"~3s later")
        print(f"    estimated cost: ~{est:.2f} {acct.currency} "
              f"(one spread x {args.lots} lot) + broker commission")
        ans = input(f"    type the account number ({acct.login}) to "
                    f"confirm, anything else aborts: ")
        if ans.strip() != str(acct.login):
            mt5.shutdown()
            raise SystemExit("confirmation did not match - aborted, "
                             "NO order was sent")
        tick = mt5.symbol_info_tick(args.symbol)   # refresh after the pause

    if getattr(info, "volume_min", 0.0) and args.lots < info.volume_min:
        raise SystemExit(f"--lots {args.lots} is below this broker's minimum "
                         f"of {info.volume_min} for {args.symbol}")

    if args.max_spread is not None:
        for _ in range(15):                    # give it up to 15s to tighten
            tick = mt5.symbol_info_tick(args.symbol)
            spread = tick.ask - tick.bid
            if spread <= args.max_spread:
                break
            print(f"    spread {spread:.2f} > cap {args.max_spread}, "
                  f"waiting for it to tighten...")
            time.sleep(1)
        else:
            mt5.shutdown()
            raise SystemExit(f"spread stayed above cap {args.max_spread} "
                             f"for 15s - test order NOT sent")

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
