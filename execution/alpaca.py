"""Alpaca paper-account broker via the official alpaca-py SDK.

Hard-wired to paper=True. If ALPACA_PAPER_TRADE is anything but true the
constructor refuses - this project never trades live on Alpaca.
"""
import os
from datetime import date

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType, OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest

from execution.base import Contract, ExecutionBroker, OrderResult, Quote


class AlpacaPaperBroker(ExecutionBroker):
    name = 'alpaca_paper'

    def __init__(self, api_key=None, secret_key=None):
        api_key = api_key or os.getenv('ALPACA_API_KEY')
        secret_key = secret_key or os.getenv('ALPACA_SECRET_KEY')
        if not api_key or not secret_key:
            raise RuntimeError('ALPACA_API_KEY / ALPACA_SECRET_KEY not configured')
        if os.getenv('ALPACA_PAPER_TRADE', 'true').strip().lower() != 'true':
            raise RuntimeError('ALPACA_PAPER_TRADE must be true; live Alpaca trading is not supported')
        self.trading = TradingClient(api_key, secret_key, paper=True)
        self.data = OptionHistoricalDataClient(api_key, secret_key)

    # ---- reference data --------------------------------------------------

    def resolve_contract(self, ticker, option_type, strike, expiration):
        req = GetOptionContractsRequest(
            underlying_symbols=[ticker.upper()],
            expiration_date=date.fromisoformat(expiration),
            type=ContractType.CALL if option_type == 'call' else ContractType.PUT,
            strike_price_gte=str(strike),
            strike_price_lte=str(strike),
        )
        resp = self.trading.get_option_contracts(req)
        contracts = resp.option_contracts or []
        if not contracts:
            return None
        c = contracts[0]
        return Contract(
            symbol=c.symbol, ticker=ticker.upper(), option_type=option_type,
            strike=float(c.strike_price), expiration=str(c.expiration_date),
            tradable=bool(c.tradable),
            open_interest=int(c.open_interest) if c.open_interest else None,
        )

    def get_quote(self, contract):
        snaps = self.data.get_option_snapshot(
            OptionSnapshotRequest(symbol_or_symbols=contract.symbol))
        s = snaps.get(contract.symbol)
        if s is None or s.latest_quote is None:
            return None
        q, g = s.latest_quote, s.greeks
        return Quote(
            bid=float(q.bid_price), ask=float(q.ask_price),
            bid_size=q.bid_size, ask_size=q.ask_size, timestamp=str(q.timestamp),
            delta=g.delta if g else None, gamma=g.gamma if g else None,
            theta=g.theta if g else None, vega=g.vega if g else None,
            rho=g.rho if g else None, iv=s.implied_volatility,
        )

    # ---- orders ----------------------------------------------------------

    def _submit(self, contract, qty, limit_price, side, intent, client_order_id):
        req = LimitOrderRequest(
            symbol=contract.symbol, qty=qty, side=side,
            time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
            position_intent=intent, client_order_id=client_order_id,
        )
        return self._to_result(self.trading.submit_order(req))

    def buy_to_open(self, contract, qty, limit_price, client_order_id=None):
        return self._submit(contract, qty, limit_price, OrderSide.BUY,
                            PositionIntent.BUY_TO_OPEN, client_order_id)

    def sell_to_close(self, contract, qty, limit_price, client_order_id=None):
        return self._submit(contract, qty, limit_price, OrderSide.SELL,
                            PositionIntent.SELL_TO_CLOSE, client_order_id)

    def get_order(self, broker_order_id):
        return self._to_result(self.trading.get_order_by_id(broker_order_id))

    def cancel_order(self, broker_order_id):
        self.trading.cancel_order_by_id(broker_order_id)

    def positions(self):
        out = []
        for p in self.trading.get_all_positions():
            out.append({
                'symbol': p.symbol, 'qty': int(float(p.qty)),
                'avg_cost': float(p.avg_entry_price),
                'market_value': float(p.market_value) if p.market_value else None,
                'unrealized_pl': float(p.unrealized_pl) if p.unrealized_pl else None,
            })
        return out

    @staticmethod
    def _enum(v):
        return str(v.value if hasattr(v, 'value') else v).lower()

    @classmethod
    def _to_result(cls, o):
        return OrderResult(
            broker_order_id=str(o.id),
            client_order_id=o.client_order_id,
            status=cls._enum(o.status),
            symbol=o.symbol,
            side=cls._enum(o.side),
            qty=int(float(o.qty)) if o.qty else 0,
            limit_price=float(o.limit_price) if o.limit_price else None,
            filled_qty=int(float(o.filled_qty)) if o.filled_qty else 0,
            fill_price=float(o.filled_avg_price) if o.filled_avg_price else None,
            submitted_at=str(o.submitted_at) if o.submitted_at else None,
            filled_at=str(o.filled_at) if o.filled_at else None,
            raw=o,
        )
