"""Translate the source's position actions into contract counts for ours.

Pure functions, no DB, no broker. Phase 3 wires these to real orders.

Rules (PLAN.md section 2.4):
  * trim fraction f  -> sell round(remaining * f), but never the last contract
                        (that's the runner); only `close` sells the last one.
  * trim of 1 contract remaining -> no-op.
  * trim contracts=n -> sell min(n, remaining - 1).
  * close            -> sell everything remaining.
  * stop_pct         -> absolute price from OUR average cost, not the source's.
"""
from dataclasses import dataclass


@dataclass
class Position:
    qty_opened: int
    remaining_qty: int
    avg_cost: float
    stop_price: float | None = None
    trimmed: bool = False

    @property
    def is_open(self) -> bool:
        return self.remaining_qty > 0


def contracts_for_trim(remaining_qty: int, fraction: float | None,
                       contracts: int | None = None) -> int:
    """How many contracts to sell for a trim. 0 means 'nothing to do'."""
    if remaining_qty <= 1:
        return 0
    if contracts is not None:
        return max(0, min(contracts, remaining_qty - 1))
    if fraction is None:
        return 0
    n = round(remaining_qty * fraction)
    n = min(n, remaining_qty - 1)   # keep the runner
    return max(n, 1) if fraction > 0 else 0


def contracts_for_close(remaining_qty: int) -> int:
    return max(0, remaining_qty)


def stop_price_from(avg_cost: float, stop_price: float | None,
                    stop_pct: float | None) -> float | None:
    """Absolute stop for our position. A % stop is measured from our fill."""
    if stop_price is not None:
        return round(stop_price, 2)
    if stop_pct is not None:
        return round(avg_cost * (1 - stop_pct / 100), 2)
    return None


def breakeven_runner_stop(pos: Position) -> float | None:
    """After a trim, runners must not go red: stop at our avg cost."""
    return round(pos.avg_cost, 2) if pos.trimmed and pos.is_open else None


def apply_update(pos: Position, action: str, fraction=None, contracts=None,
                 stop_price=None, stop_pct=None) -> tuple[Position, int]:
    """Return (new position, contracts to sell). Does not touch price/fills -
    the broker layer reports those back and updates avg_cost/realized P&L."""
    if not pos.is_open:
        return pos, 0

    if action == 'trim':
        n = contracts_for_trim(pos.remaining_qty, fraction, contracts)
        if n:
            pos = Position(pos.qty_opened, pos.remaining_qty - n, pos.avg_cost,
                           stop_price=pos.stop_price, trimmed=True)
        return pos, n

    if action == 'close':
        n = contracts_for_close(pos.remaining_qty)
        pos = Position(pos.qty_opened, 0, pos.avg_cost,
                       stop_price=pos.stop_price, trimmed=pos.trimmed)
        return pos, n

    if action == 'stop':
        new_stop = stop_price_from(pos.avg_cost, stop_price, stop_pct)
        # The source can tighten our stop, never loosen it.
        if new_stop is not None and (pos.stop_price is None or new_stop > pos.stop_price):
            pos = Position(pos.qty_opened, pos.remaining_qty, pos.avg_cost,
                           stop_price=new_stop, trimmed=pos.trimmed)
        return pos, 0

    # price_update / add / note: no sell. (`add` buys are a Phase 3 sizing
    # decision, capped by max position size - not modelled here.)
    return pos, 0
