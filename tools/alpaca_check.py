"""Verify the Alpaca paper account is reachable and options-enabled.

    python -m tools.alpaca_check            # account + options level
    python -m tools.alpaca_check SPY 754 put 2026-09-16   # also resolve a contract + quote/greeks

Reads ALPACA_API_KEY / ALPACA_SECRET_KEY from .env. Never places an order.
"""
import os
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv()

KEY = os.getenv('ALPACA_API_KEY')
SECRET = os.getenv('ALPACA_SECRET_KEY')
PAPER = os.getenv('ALPACA_PAPER_TRADE', 'true').strip().lower() != 'false'


def main():
    if not KEY or not SECRET:
        print('ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env')
        sys.exit(1)
    if not PAPER:
        print('Refusing to run: ALPACA_PAPER_TRADE is not true. This project only uses paper.')
        sys.exit(1)

    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.trading.enums import ContractType
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionSnapshotRequest

    trading = TradingClient(KEY, SECRET, paper=True)
    acct = trading.get_account()
    print(f'account           {acct.account_number}  status={acct.status}')
    print(f'paper             {PAPER}')
    print(f'equity            {acct.equity}')
    print(f'options buying pw {acct.options_buying_power}')
    print(f'options approved  level {acct.options_approved_level}   trading level {acct.options_trading_level}')
    if not acct.options_trading_level or int(acct.options_trading_level) < 2:
        print('!! options trading level < 2: long calls/puts will be rejected. '
              'Enable options on the paper account in the Alpaca dashboard.')

    clock = trading.get_clock()
    print(f'market            {"OPEN" if clock.is_open else "closed"}  next open {clock.next_open}')

    if len(sys.argv) >= 5:
        ticker, strike, otype, exp = sys.argv[1].upper(), float(sys.argv[2]), sys.argv[3].lower(), sys.argv[4]
        req = GetOptionContractsRequest(
            underlying_symbols=[ticker],
            expiration_date=date.fromisoformat(exp),
            type=ContractType.CALL if otype.startswith('c') else ContractType.PUT,
            strike_price_gte=str(strike), strike_price_lte=str(strike),
        )
        resp = trading.get_option_contracts(req)
        contracts = resp.option_contracts or []
        if not contracts:
            print(f'no contract found for {ticker} {strike:g} {otype} {exp}')
            return
        c = contracts[0]
        print(f'contract          {c.symbol}  tradable={c.tradable}  oi={c.open_interest}  '
              f'close={c.close_price}')

        data = OptionHistoricalDataClient(KEY, SECRET)
        snap = data.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=c.symbol))
        s = snap.get(c.symbol)
        if s is None:
            print('no snapshot returned')
            return
        q = s.latest_quote
        g = s.greeks
        print(f'quote             bid {q.bid_price} x{q.bid_size}  ask {q.ask_price} x{q.ask_size}  '
              f'@ {q.timestamp}')
        if g:
            print(f'greeks            delta {g.delta:.4f} gamma {g.gamma:.4f} theta {g.theta:.4f} '
                  f'vega {g.vega:.4f} rho {g.rho:.4f}   IV {s.implied_volatility}')
        else:
            print('greeks            (none returned - usually means outside market hours or no trades yet)')


if __name__ == '__main__':
    main()
