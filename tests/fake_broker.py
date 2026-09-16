"""In-memory broker for runner tests. Fills are explicit: call fill()."""
import itertools

from execution.base import Contract, ExecutionBroker, OrderResult, Quote


class FakeBroker(ExecutionBroker):
    name = 'fake'

    def __init__(self):
        self.quotes: dict[str, Quote] = {}
        self.orders: dict[str, dict] = {}
        self.open_interest = 1000
        self._ids = itertools.count(1)
        self.calls: list[tuple] = []

    # ---- test helpers -------------------------------------------------
    @staticmethod
    def symbol(ticker, option_type, strike, expiration):
        y, m, d = expiration.split('-')
        return f"{ticker}{y[2:]}{m}{d}{'C' if option_type == 'call' else 'P'}{int(strike * 1000):08d}"

    def set_quote(self, symbol, bid, ask, **greeks):
        self.quotes[symbol] = Quote(bid=bid, ask=ask, bid_size=10, ask_size=10,
                                    timestamp='t', **greeks)

    def fill(self, broker_order_id, price=None, qty=None):
        o = self.orders[broker_order_id]
        qty = qty if qty is not None else o['qty'] - o['filled_qty']
        price = price if price is not None else o['limit_price']
        o['filled_qty'] += qty
        o['fill_price'] = price
        o['status'] = 'filled' if o['filled_qty'] >= o['qty'] else 'partially_filled'
        o['filled_at'] = 't'

    def last_order(self):
        return self.orders[max(self.orders, key=int)]

    # ---- ExecutionBroker ------------------------------------------------
    def resolve_contract(self, ticker, option_type, strike, expiration):
        sym = self.symbol(ticker, option_type, strike, expiration)
        if sym not in self.quotes:
            return None
        return Contract(sym, ticker, option_type, strike, expiration, True, self.open_interest)

    def get_quote(self, contract):
        return self.quotes.get(contract.symbol)

    def _submit(self, contract, qty, limit, side, coid):
        oid = str(next(self._ids))
        self.orders[oid] = dict(id=oid, coid=coid, symbol=contract.symbol, side=side, qty=qty,
                                limit_price=limit, status='accepted', filled_qty=0,
                                fill_price=None, filled_at=None)
        self.calls.append((side, contract.symbol, qty, limit))
        return self.get_order(oid)

    def buy_to_open(self, contract, qty, limit_price, client_order_id=None):
        return self._submit(contract, qty, limit_price, 'buy', client_order_id)

    def sell_to_close(self, contract, qty, limit_price, client_order_id=None):
        return self._submit(contract, qty, limit_price, 'sell', client_order_id)

    def get_order(self, broker_order_id):
        o = self.orders[broker_order_id]
        return OrderResult(o['id'], o['coid'], o['status'], o['symbol'], o['side'], o['qty'],
                           o['limit_price'], o['filled_qty'], o['fill_price'], 't', o['filled_at'])

    def cancel_order(self, broker_order_id):
        o = self.orders[broker_order_id]
        if o['status'] not in ('filled',):
            o['status'] = 'canceled'

    def positions(self):
        return []
