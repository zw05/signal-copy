"""Drive the Alpaca paper broker by hand - the lifecycle the runner will automate.

    python -m tools.paper_trade open SPY 754 put 2026-09-17 --qty 3
    python -m tools.paper_trade status <order_id>
    python -m tools.paper_trade trim  SPY 754 put 2026-09-17 --fraction 0.75
    python -m tools.paper_trade close SPY 754 put 2026-09-17
    python -m tools.paper_trade cancel <order_id>
    python -m tools.paper_trade positions

Buys are limit-at-ask, sells are limit-at-bid (marketable), DAY orders.
Paper account only - the broker class refuses anything else.
"""
import argparse
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

from execution.alpaca import AlpacaPaperBroker  # noqa: E402
from positions import contracts_for_trim  # noqa: E402


def _print_order(o):
    print(f'  order   {o.broker_order_id}')
    print(f'  status  {o.status}')
    print(f'  {o.side} {o.qty} x {o.symbol} @ limit {o.limit_price}')
    print(f'  filled  {o.filled_qty} @ {o.fill_price}   submitted {o.submitted_at}')


def _resolve(broker, a):
    c = broker.resolve_contract(a.ticker, a.type, a.strike, a.expiration)
    if c is None:
        sys.exit(f'no contract for {a.ticker} {a.strike:g} {a.type} {a.expiration}')
    q = broker.get_quote(c)
    if q is None:
        sys.exit(f'no quote for {c.symbol}')
    print(f'contract {c.symbol}  tradable={c.tradable}  OI={c.open_interest}')
    print(f'quote    bid {q.bid} / ask {q.ask}  mark {q.mark}  spread {q.spread_pct}%  '
          f'delta {q.delta}  @ {q.timestamp}')
    return c, q


def _held_qty(broker, symbol):
    for p in broker.positions():
        if p['symbol'] == symbol:
            return p['qty'], p['avg_cost']
    return 0, None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    def contract_args(p):
        p.add_argument('ticker'); p.add_argument('strike', type=float)
        p.add_argument('type', choices=['call', 'put']); p.add_argument('expiration')

    p = sub.add_parser('open'); contract_args(p)
    p.add_argument('--qty', type=int, default=1)
    p.add_argument('--limit', type=float, help='override limit (default: ask)')
    p = sub.add_parser('trim'); contract_args(p)
    p.add_argument('--fraction', type=float, default=0.75)
    p = sub.add_parser('close'); contract_args(p)
    p = sub.add_parser('status'); p.add_argument('order_id')
    p = sub.add_parser('cancel'); p.add_argument('order_id')
    sub.add_parser('positions')
    a = ap.parse_args()

    broker = AlpacaPaperBroker()

    if a.cmd == 'positions':
        rows = broker.positions()
        if not rows:
            print('no open positions')
        for r in rows:
            print(f"  {r['symbol']:<22} qty {r['qty']}  avg {r['avg_cost']}  "
                  f"mv {r['market_value']}  upl {r['unrealized_pl']}")
        return

    if a.cmd == 'status':
        _print_order(broker.get_order(a.order_id)); return

    if a.cmd == 'cancel':
        broker.cancel_order(a.order_id)
        print('cancel requested'); _print_order(broker.get_order(a.order_id)); return

    c, q = _resolve(broker, a)

    if a.cmd == 'open':
        limit = a.limit or q.ask
        print(f'BUY TO OPEN {a.qty} x {c.symbol} @ {limit}  (~${limit * 100 * a.qty:.0f} paper)')
        o = broker.buy_to_open(c, a.qty, limit, client_order_id=f'manual-{uuid.uuid4().hex[:12]}')
        _print_order(o); return

    held, avg = _held_qty(broker, c.symbol)
    if held <= 0:
        sys.exit(f'no position in {c.symbol} to sell')
    print(f'holding  {held} @ avg {avg}')

    if a.cmd == 'trim':
        n = contracts_for_trim(held, a.fraction)
        if n == 0:
            sys.exit(f'trim of {a.fraction} on {held} contract(s) is a no-op (runner rule)')
        print(f'SELL TO CLOSE {n} of {held} x {c.symbol} @ {q.bid}  (keeping {held - n} runner)')
    else:
        n = held
        print(f'SELL TO CLOSE {n} x {c.symbol} @ {q.bid}  (full close)')
    o = broker.sell_to_close(c, n, q.bid, client_order_id=f'manual-{uuid.uuid4().hex[:12]}')
    _print_order(o)


if __name__ == '__main__':
    main()
