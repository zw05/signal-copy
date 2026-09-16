"""Broker interface the runner talks to. Phase 3: AlpacaPaperBroker.
Phase 4: RobinhoodBroker. The runner never knows which one it has."""
from dataclasses import dataclass


@dataclass
class Contract:
    symbol: str            # broker symbol (Alpaca: OCC, e.g. SPY260917P00754000)
    ticker: str
    option_type: str       # 'call' | 'put'
    strike: float
    expiration: str        # YYYY-MM-DD
    tradable: bool
    open_interest: int | None = None


@dataclass
class Quote:
    bid: float
    ask: float
    bid_size: float | None
    ask_size: float | None
    timestamp: str
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    iv: float | None = None

    @property
    def mark(self) -> float:
        return round((self.bid + self.ask) / 2, 2)

    @property
    def spread_pct(self) -> float | None:
        return round((self.ask - self.bid) / self.mark * 100, 1) if self.mark else None


@dataclass
class OrderResult:
    broker_order_id: str
    client_order_id: str | None
    status: str            # broker status, lower-cased
    symbol: str
    side: str              # 'buy' | 'sell'
    qty: int
    limit_price: float | None
    filled_qty: int
    fill_price: float | None
    submitted_at: str | None
    filled_at: str | None
    raw: object = None

    @property
    def is_filled(self) -> bool:
        return self.status == 'filled'

    @property
    def is_terminal(self) -> bool:
        return self.status in ('filled', 'canceled', 'cancelled', 'expired', 'rejected')


class ExecutionBroker:
    name: str

    def resolve_contract(self, ticker: str, option_type: str, strike: float,
                         expiration: str) -> Contract | None:
        raise NotImplementedError

    def get_quote(self, contract: Contract) -> Quote | None:
        raise NotImplementedError

    def buy_to_open(self, contract: Contract, qty: int, limit_price: float,
                    client_order_id: str | None = None) -> OrderResult:
        raise NotImplementedError

    def sell_to_close(self, contract: Contract, qty: int, limit_price: float,
                      client_order_id: str | None = None) -> OrderResult:
        raise NotImplementedError

    def get_order(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError

    def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError

    def positions(self) -> list[dict]:
        raise NotImplementedError
