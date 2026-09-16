import os
from datetime import datetime, timedelta, timezone

import pytest

import database
from database import (
    get_order,
    get_position_by_signal,
    get_signal,
    get_signal_updates,
    init_db,
    insert_message,
)
from pipeline import process_message
from rules import Rules
from runner import Runner
from tests.fake_broker import FakeBroker

MIKE = '111'
ENTRY = """$SPY
$754 PUTS
 EXPIRATION 9/16/2026
$.68 Entry
@everyone

seeking for these to go ITM with a $755 PT."""
SYM = FakeBroker.symbol('SPY', 'put', 754.0, '2026-09-16')

# 14:20 ET on 9/16 = 18:20 UTC
T0 = datetime(2026, 9, 16, 18, 20, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_PATH', str(tmp_path / 'test.db'))
    monkeypatch.chdir(tmp_path)  # kill switch file lands here
    init_db()


@pytest.fixture
def world():
    broker = FakeBroker()
    broker.set_quote(SYM, bid=0.66, ask=0.70, delta=-0.45, iv=0.2)
    clock = Clock(T0)
    rules = Rules(contracts_per_entry=3, max_daily_loss=500, kill_switch_file='KILL')
    runner = Runner(broker, rules, now_fn=clock)
    return broker, clock, runner


def post(discord_id, content, at: datetime, reply_to=None):
    mid, _ = insert_message(
        discord_id=discord_id, guild_id='1', channel_id='9', author_id=MIKE,
        author_name='mike', content=content, reply_to_id=reply_to,
        created_at=at.isoformat(),
    )
    return process_message(database.get_message(mid), use_llm=False)


def entry(clock, sid='e1'):
    r = post(sid, ENTRY, clock.t)
    return r['signal_ids'][0]


# --------------------------------------------------------------------------

def test_entry_is_bought_at_ask_within_chase(world):
    broker, clock, runner = world
    sig = entry(clock)
    ev = runner.tick()
    pos = get_position_by_signal(sig)
    assert pos['status'] == 'opening' and pos['qty_target'] == 3
    assert broker.calls == [('buy', SYM, 3, 0.70)]
    assert get_signal(sig)['trade_status'] == 'submitted'
    assert any(ev_.startswith('BUY 3') for ev_ in ev)


def test_entry_limit_capped_at_source_plus_chase(world):
    broker, clock, runner = world
    broker.set_quote(SYM, bid=0.72, ask=0.74)   # cap = .68 * 1.10 = .748 -> ask ok
    entry(clock)
    runner.tick()
    assert broker.calls[0][3] == 0.74
    broker.calls.clear()


def test_entry_too_expensive_is_missed(world):
    broker, clock, runner = world
    broker.set_quote(SYM, bid=0.80, ask=0.85)
    sig = entry(clock)
    runner.tick()
    assert broker.calls == []
    assert get_signal(sig)['trade_status'] == 'missed'
    assert get_position_by_signal(sig)['status'] == 'missed'


def test_stale_signal_is_missed(world):
    broker, clock, runner = world
    sig = entry(clock)
    clock.advance(minutes=4)
    runner.tick()
    assert broker.calls == []
    assert get_signal(sig)['trade_status'] == 'missed'


def test_illiquid_is_skipped(world):
    broker, clock, runner = world
    broker.open_interest = 5
    sig = entry(clock)
    runner.tick()
    assert broker.calls == []
    assert get_signal(sig)['trade_status'] == 'skipped'


def test_full_lifecycle_trim_then_close(world):
    broker, clock, runner = world
    sig = entry(clock)
    runner.tick()
    broker.fill(broker.last_order()['id'], price=0.70)
    clock.advance(seconds=15)
    runner.tick()
    pos = get_position_by_signal(sig)
    assert pos['status'] == 'open' and pos['remaining_qty'] == 3 and pos['avg_cost'] == 0.70
    assert pos['stop_price'] == 0.35          # hard stop -50%
    assert get_signal(sig)['trade_status'] == 'opened'

    # price update: no order
    clock.advance(minutes=15)
    broker.set_quote(SYM, bid=0.99, ask=1.02)
    post('u1', '$.99 HERE ON SPY PUTS\nUP +48%', clock.t, reply_to='e1')
    runner.tick()
    assert len(broker.calls) == 1

    # SOLD MAJORITY -> sell 2 at bid, keep runner, stop moves to break-even
    clock.advance(seconds=30)
    post('u2', 'SOLD MAJORITY 🚨🚨🚨', clock.t)
    runner.tick()
    assert broker.calls[-1] == ('sell', SYM, 2, 0.99)
    broker.fill(broker.last_order()['id'], price=0.99)
    clock.advance(seconds=15)
    runner.tick()
    pos = get_position_by_signal(sig)
    assert pos['remaining_qty'] == 1 and pos['trimmed'] == 1
    assert pos['stop_price'] == 0.70
    assert pos['realized_pnl'] == pytest.approx((0.99 - 0.70) * 100 * 2)

    # second trim on the runner: no-op
    clock.advance(minutes=2)
    post('u3', 'sold some more', clock.t)
    runner.tick()
    assert len([c for c in broker.calls if c[0] == 'sell']) == 1

    # ALL OUT -> sell the runner
    clock.advance(minutes=2)
    broker.set_quote(SYM, bid=1.36, ask=1.40)
    post('u4', 'ALL OUT 🔥', clock.t)
    runner.tick()
    assert broker.calls[-1] == ('sell', SYM, 1, 1.36)
    broker.fill(broker.last_order()['id'], price=1.36)
    clock.advance(seconds=15)
    runner.tick()
    pos = get_position_by_signal(sig)
    assert pos['status'] == 'closed' and pos['remaining_qty'] == 0
    assert pos['exit_reason'] == 'close'
    assert pos['realized_pnl'] == pytest.approx(58 + 66)

    statuses = {u['action']: u['execution_status'] for u in get_signal_updates(sig)}
    assert statuses == {'price_update': 'noop', 'trim': 'noop', 'close': 'executed'} or \
        [u['execution_status'] for u in get_signal_updates(sig)] == ['noop', 'executed', 'noop', 'executed']


def test_runner_breakeven_stop(world):
    broker, clock, runner = world
    sig = entry(clock)
    runner.tick()
    broker.fill(broker.last_order()['id'], price=0.70)
    runner.tick()
    broker.set_quote(SYM, bid=1.00, ask=1.04)
    post('u1', 'SOLD MAJORITY', clock.t + timedelta(seconds=5))
    clock.advance(seconds=10)
    runner.tick()
    broker.fill(broker.last_order()['id'], price=1.00)
    runner.tick()
    # runner falls back to our cost -> sold, reason runner_breakeven
    broker.set_quote(SYM, bid=0.69, ask=0.72)
    runner.tick()
    assert broker.calls[-1] == ('sell', SYM, 1, 0.69)
    assert get_position_by_signal(sig)['exit_reason'] == 'runner_breakeven'


def test_hard_stop_without_source_stop(world):
    broker, clock, runner = world
    sig = entry(clock)
    runner.tick()
    broker.fill(broker.last_order()['id'], price=0.70)
    runner.tick()
    broker.set_quote(SYM, bid=0.34, ask=0.38)
    runner.tick()
    assert broker.calls[-1] == ('sell', SYM, 3, 0.34)
    assert get_position_by_signal(sig)['exit_reason'] == 'stop'


def test_source_stop_tightens_only(world):
    broker, clock, runner = world
    sig = entry(clock)
    runner.tick()
    broker.fill(broker.last_order()['id'], price=0.70)
    runner.tick()
    post('u1', 'stop at .25', clock.t + timedelta(seconds=5))     # looser than .35 -> ignored
    post('u2', 'stop at .50', clock.t + timedelta(seconds=6))
    clock.advance(seconds=10)
    runner.tick()
    assert get_position_by_signal(sig)['stop_price'] == 0.50


def test_time_stop_flattens_0dte(world):
    broker, clock, runner = world
    sig = entry(clock)
    runner.tick()
    broker.fill(broker.last_order()['id'], price=0.70)
    runner.tick()
    # 15:45 ET = 19:45 UTC on 9/16
    clock.t = datetime(2026, 9, 16, 19, 45, tzinfo=timezone.utc)
    broker.set_quote(SYM, bid=0.90, ask=0.95)
    runner.tick()
    assert broker.calls[-1] == ('sell', SYM, 3, 0.90)
    assert get_position_by_signal(sig)['exit_reason'] == 'time_stop'


def test_entry_ttl_cancels_and_marks_missed(world):
    broker, clock, runner = world
    sig = entry(clock)
    runner.tick()
    clock.advance(minutes=6)
    runner.tick()
    assert broker.last_order()['status'] == 'canceled'
    assert get_position_by_signal(sig)['status'] == 'missed'
    assert get_signal(sig)['trade_status'] == 'missed'


def test_source_exits_before_our_fill(world):
    broker, clock, runner = world
    sig = entry(clock)
    runner.tick()
    post('u1', 'SOLD MAJORITY', clock.t + timedelta(seconds=30))
    clock.advance(seconds=31)
    runner.tick()
    assert broker.last_order()['status'] == 'canceled'
    assert get_position_by_signal(sig)['exit_reason'] == 'source_exited_before_fill'
    assert get_signal_updates(sig)[0]['execution_status'] == 'skipped'


def test_sell_ttl_reprices(world):
    broker, clock, runner = world
    sig = entry(clock)
    runner.tick()
    broker.fill(broker.last_order()['id'], price=0.70)
    runner.tick()
    post('u1', 'ALL OUT', clock.t + timedelta(seconds=5))
    clock.advance(seconds=10)
    runner.tick()
    first = broker.last_order()['id']
    clock.advance(seconds=90)
    broker.set_quote(SYM, bid=0.60, ask=0.64)
    runner.tick()
    assert broker.orders[first]['status'] == 'canceled'
    assert broker.calls[-1] == ('sell', SYM, 3, 0.60)


def test_daily_loss_halts_and_writes_kill_file(world):
    broker, clock, runner = world
    sig = entry(clock)
    runner.tick()
    broker.fill(broker.last_order()['id'], price=0.70)
    runner.tick()
    # 3 contracts, cost .70, bid .36 -> unrealized -102; stop at .35 not yet hit
    # lower the cap so it trips
    runner.rules = Rules(contracts_per_entry=3, max_daily_loss=100, kill_switch_file='KILL')
    broker.set_quote(SYM, bid=0.36, ask=0.40)
    ev = runner.tick()
    assert runner.halted
    assert os.path.exists('KILL')
    assert broker.calls[-1] == ('sell', SYM, 3, 0.36)
    assert any(e.startswith('HALT') for e in ev)
    # halted runner does nothing more
    assert runner.tick() == []


def test_kill_switch_file_halts(world):
    broker, clock, runner = world
    open('KILL', 'w').close()
    entry(clock)
    ev = runner.tick()
    assert runner.halted and broker.calls == []
    assert ev and ev[0].startswith('HALT')


def test_max_open_positions(world):
    broker, clock, runner = world
    runner.rules = Rules(contracts_per_entry=3, max_open_positions=1, kill_switch_file='KILL')
    s1 = entry(clock, 'e1')
    s2 = post('e2', ENTRY.replace('$754 PUTS', '$760 CALLS'), clock.t)['signal_ids'][0]
    broker.set_quote(FakeBroker.symbol('SPY', 'call', 760.0, '2026-09-16'), bid=0.5, ask=0.55)
    runner.tick()
    assert get_signal(s1)['trade_status'] == 'submitted'
    assert get_signal(s2)['trade_status'] == 'skipped'
