import pytest

from positions import (
    Position,
    apply_update,
    breakeven_runner_stop,
    contracts_for_trim,
    stop_price_from,
)


@pytest.mark.parametrize('remaining,fraction,expected', [
    (3, 0.75, 2),   # SOLD MAJORITY with 3 -> sell 2, keep 1 runner
    (2, 0.75, 1),   # with 2 -> sell 1, keep 1
    (1, 0.75, 0),   # can't trim 1 contract
    (3, 0.5, 2),    # round(1.5)=2 -> sell 2, keep 1
    (4, 0.5, 2),
    (3, 1 / 3, 1),
    (4, 0.25, 1),
    (5, 0.9, 4),    # never the last one
    (2, 0.1, 1),    # tiny fraction still sells at least one when >1 remain
    (3, 0.0, 0),
])
def test_trim_fraction(remaining, fraction, expected):
    assert contracts_for_trim(remaining, fraction) == expected


def test_trim_contracts_capped_to_keep_runner():
    assert contracts_for_trim(3, None, contracts=2) == 2
    assert contracts_for_trim(3, None, contracts=5) == 2
    assert contracts_for_trim(1, None, contracts=1) == 0


def test_stop_from_pct_uses_our_cost():
    assert stop_price_from(0.70, None, 20) == 0.56
    assert stop_price_from(0.70, 0.25, 20) == 0.25


def test_lifecycle_3_contracts():
    pos = Position(qty_opened=3, remaining_qty=3, avg_cost=0.70)
    pos, n = apply_update(pos, 'price_update')
    assert n == 0 and pos.remaining_qty == 3
    pos, n = apply_update(pos, 'trim', fraction=0.75)
    assert n == 2 and pos.remaining_qty == 1 and pos.trimmed
    assert breakeven_runner_stop(pos) == 0.70
    pos, n = apply_update(pos, 'trim', fraction=0.5)     # second trim on the runner: no-op
    assert n == 0 and pos.remaining_qty == 1
    pos, n = apply_update(pos, 'close')
    assert n == 1 and not pos.is_open
    pos, n = apply_update(pos, 'close')                   # idempotent
    assert n == 0


def test_stop_can_tighten_not_loosen():
    pos = Position(3, 3, 0.70, stop_price=0.50)
    pos, _ = apply_update(pos, 'stop', stop_price=0.40)
    assert pos.stop_price == 0.50
    pos, _ = apply_update(pos, 'stop', stop_price=0.60)
    assert pos.stop_price == 0.60
    pos, _ = apply_update(pos, 'stop', stop_pct=10)      # 0.63 from our cost
    assert pos.stop_price == 0.63
