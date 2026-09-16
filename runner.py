"""The copy-trading loop: signals + updates -> rules -> broker -> positions.

One `tick()` does, in order:
  1. kill switch check
  2. poll working orders, apply fills to positions
  3. new entry signals   -> buy-to-open
  4. new signal updates  -> trim / close / stop
  5. open positions      -> stop, runner break-even stop, time stop
  6. daily-loss check    -> halt + flatten

Everything the tick decides is written to `orders` / `positions` and returned
as a list of human-readable events for the notification channel.

    python -m runner            # standalone loop against the Alpaca paper account
    python -m runner --once     # single tick
"""
import logging
import time
import uuid
from datetime import datetime, timezone

from database import (
    get_order,
    get_position,
    get_position_by_signal,
    get_positions,
    get_realized_pnl_since,
    get_signals_awaiting_entry,
    get_pending_updates,
    get_working_orders,
    insert_order,
    insert_position,
    mark_update_executed,
    update_order,
    update_position,
    update_signal_trade_status,
)
from execution.base import ExecutionBroker
from positions import contracts_for_trim, stop_price_from
from rules import Rules

log = logging.getLogger('stockbot.runner')

TERMINAL = ('filled', 'canceled', 'cancelled', 'expired', 'rejected')


def _utc(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Runner:
    def __init__(self, broker: ExecutionBroker, rules: Rules, now_fn=None):
        self.broker = broker
        self.rules = rules
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.halted = False
        self.events: list[str] = []

    # ------------------------------------------------------------------
    def tick(self) -> list[str]:
        self.events = []
        if self.halted or self.rules.kill_switch_on():
            if not self.halted:
                self._halt('kill switch file present')
            return self.events
        for step in (self._poll_orders, self._process_entries, self._process_updates,
                     self._poll_positions, self._check_daily_loss):
            try:
                step()
            except Exception:  # noqa: BLE001 - one bad step must not stop the loop
                log.exception('runner step %s failed', step.__name__)
                self._event(f'ERROR in {step.__name__}, see log')
        return self.events

    def _event(self, text: str):
        log.info(text)
        self.events.append(text)

    # ------------------------------------------------------------------
    # 1. orders
    # ------------------------------------------------------------------
    def _poll_orders(self):
        now = self._now()
        for o in get_working_orders():
            try:
                res = self.broker.get_order(o['broker_order_id'])
            except Exception as exc:  # noqa: BLE001
                log.warning('get_order %s failed: %s', o['broker_order_id'], exc)
                continue
            self._apply_order_state(o, res)

            age = (now - _utc(o['submitted_at'])).total_seconds() if o['submitted_at'] else 0
            if res.status in TERMINAL:
                continue
            if o['side'] == 'buy' and age > self.rules.entry_order_ttl_minutes * 60:
                self._cancel(o, 'entry ttl')
            elif o['side'] == 'sell' and age > self.rules.sell_order_ttl_seconds:
                # Re-price at the current bid; the position poll / update
                # replay will resubmit for whatever is still unsold.
                self._cancel(o, 'sell ttl')
                pos = get_position(o['position_id'])
                if pos and pos['status'] == 'open':
                    self._sell(pos, pos['remaining_qty'] - self._unfilled_sell_qty(pos['id']),
                               o['reason'], o['update_id'])

    def _cancel(self, order_row, why):
        try:
            self.broker.cancel_order(order_row['broker_order_id'])
            res = self.broker.get_order(order_row['broker_order_id'])
            self._apply_order_state(order_row, res)
            self._event(f"cancelled {order_row['side']} order #{order_row['id']} ({why})")
        except Exception as exc:  # noqa: BLE001
            log.warning('cancel %s failed: %s', order_row['broker_order_id'], exc)

    def _apply_order_state(self, o: dict, res):
        """Persist broker state and apply any newly filled quantity."""
        newly_filled = max(0, res.filled_qty - o['filled_qty'])
        update_order(o['id'], status=res.status, filled_qty=res.filled_qty,
                     fill_price=res.fill_price, filled_at=res.filled_at)
        o['filled_qty'] = res.filled_qty
        pos = get_position(o['position_id'])
        if pos is None:
            return

        if newly_filled and res.fill_price is not None:
            if o['side'] == 'buy':
                total_cost = (pos['avg_cost'] or 0) * pos['qty_opened'] + res.fill_price * newly_filled
                qty_opened = pos['qty_opened'] + newly_filled
                avg_cost = round(total_cost / qty_opened, 4)
                fields = dict(qty_opened=qty_opened,
                              remaining_qty=pos['remaining_qty'] + newly_filled,
                              avg_cost=avg_cost, status='open')
                if pos['opened_at'] is None:
                    fields['opened_at'] = self._now().isoformat()
                if pos['stop_price'] is None:
                    fields['stop_price'] = self.rules.hard_stop_price(avg_cost)
                update_position(pos['id'], **fields)
                update_signal_trade_status(pos['signal_id'], 'opened')
                self._event(f"FILLED buy {newly_filled} x {pos['contract_symbol']} @ {res.fill_price}"
                            f"  (position #{pos['id']}, {fields['remaining_qty']} held)")
            else:
                pnl = round((res.fill_price - pos['avg_cost']) * 100 * newly_filled, 2)
                remaining = pos['remaining_qty'] - newly_filled
                fields = dict(remaining_qty=remaining,
                              realized_pnl=round(pos['realized_pnl'] + pnl, 2))
                if remaining <= 0:
                    fields.update(status='closed', closed_at=self._now().isoformat(),
                                  exit_reason=pos['exit_reason'] or o['reason'])
                update_position(pos['id'], **fields)
                self._event(f"FILLED sell {newly_filled} x {pos['contract_symbol']} @ {res.fill_price}"
                            f"  pnl {pnl:+.2f}  ({o['reason']}; {max(remaining, 0)} left)")
            pos = get_position(pos['id'])

        # Entry never filled at all -> the trade is missed.
        if (o['side'] == 'buy' and res.status in TERMINAL and not res.is_filled
                and pos['qty_opened'] == 0 and pos['status'] == 'opening'):
            reason = pos['exit_reason'] or f'entry_{res.status}'
            update_position(pos['id'], status='missed', exit_reason=reason,
                            closed_at=self._now().isoformat())
            update_signal_trade_status(pos['signal_id'], 'missed')
            self._event(f"MISSED {pos['contract_symbol']}: {reason}")

    def _unfilled_sell_qty(self, position_id) -> int:
        return sum(w['qty'] - w['filled_qty'] for w in get_working_orders(position_id, 'sell'))

    # ------------------------------------------------------------------
    # 2. entries
    # ------------------------------------------------------------------
    def _process_entries(self):
        now = self._now()
        for sig in get_signals_awaiting_entry():
            label = f"{sig['ticker']} {sig['strike']:g}{sig['option_type'][0].upper()} {sig['expiration']}"

            def skip(status, why):
                update_signal_trade_status(sig['id'], status)
                self._event(f'SKIP {label}: {why}')

            if (sig['confidence'] or 0) < self.rules.min_signal_confidence:
                skip('skipped', f"confidence {sig['confidence']}"); continue
            if not sig['expiration']:
                skip('skipped', 'no expiration'); continue
            if self.rules.is_stale(_utc(sig['message_created_at']), now):
                skip('missed', 'stale signal'); continue
            if len(get_positions(('opening', 'open'))) >= self.rules.max_open_positions:
                skip('skipped', 'max open positions'); continue

            contract = self.broker.resolve_contract(
                sig['ticker'], sig['option_type'], sig['strike'], sig['expiration'])
            if contract is None or not contract.tradable:
                skip('error', 'contract not found / not tradable'); continue
            quote = self.broker.get_quote(contract)
            if quote is None or quote.ask <= 0:
                skip('error', 'no quote'); continue
            why = self.rules.illiquid(quote.spread_pct, contract.open_interest)
            if why:
                skip('skipped', f'illiquid: {why}'); continue
            if self.rules.entry_too_expensive(sig['limit_price'], quote.ask):
                update_signal_trade_status(sig['id'], 'missed')
                insert_position(sig['id'], self.broker.name, contract.symbol,
                                self.rules.contracts_per_entry, sig['limit_price'],
                                quote.bid, quote.ask, quote.delta, quote.iv, status='missed')
                self._event(f"MISSED {label}: ask {quote.ask} > source {sig['limit_price']} "
                            f"+{self.rules.entry_chase_pct:g}%")
                continue

            limit = self.rules.entry_limit(sig['limit_price'], quote.ask)
            qty = self.rules.contracts_per_entry
            pid = insert_position(sig['id'], self.broker.name, contract.symbol, qty,
                                  sig['limit_price'], quote.bid, quote.ask, quote.delta, quote.iv)
            self._buy(pid, contract, qty, limit)
            update_signal_trade_status(sig['id'], 'submitted')

    def _buy(self, position_id, contract, qty, limit):
        coid = f'sb-{position_id}-entry-{uuid.uuid4().hex[:8]}'
        oid = insert_order(position_id, self.broker.name, 'buy', 'entry', qty, limit,
                           client_order_id=coid, submitted_at=self._now().isoformat())
        try:
            res = self.broker.buy_to_open(contract, qty, limit, client_order_id=coid)
        except Exception as exc:  # noqa: BLE001
            update_order(oid, status='error', error=str(exc)[:500])
            update_position(position_id, status='error', exit_reason='entry_error')
            self._event(f'ERROR buy {contract.symbol}: {exc}')
            return
        update_order(oid, broker_order_id=res.broker_order_id, status=res.status)
        self._event(f'BUY {qty} x {contract.symbol} @ {limit} -> {res.status}')
        self._apply_order_state(get_order(oid), res)

    # ------------------------------------------------------------------
    # 3. updates
    # ------------------------------------------------------------------
    def _process_updates(self):
        for u in get_pending_updates():
            action = u['action']
            if action in ('price_update', 'note'):
                mark_update_executed(u['id'], 'noop'); continue

            pos = get_position_by_signal(u['signal_id'])
            if pos is None:
                mark_update_executed(u['id'], 'skipped', 'no position for signal'); continue

            if pos['status'] == 'opening' and action in ('trim', 'close'):
                # Source is exiting before our entry filled: we're late. Bail.
                update_position(pos['id'], exit_reason='source_exited_before_fill')
                for w in get_working_orders(pos['id'], 'buy'):
                    self._cancel(w, 'source exited before fill')
                pos = get_position(pos['id'])
                if pos['status'] == 'opening':
                    update_position(pos['id'], status='missed', exit_reason='source_exited_before_fill',
                                    closed_at=self._now().isoformat())
                    update_signal_trade_status(pos['signal_id'], 'missed')
                    mark_update_executed(u['id'], 'skipped', 'entry not filled')
                    self._event(f"MISSED {pos['contract_symbol']}: source {action} before our fill")
                    continue

            if pos['status'] != 'open':
                mark_update_executed(u['id'], 'skipped', f"position {pos['status']}"); continue

            if action == 'stop':
                new_stop = stop_price_from(pos['avg_cost'], u['stop_price'], u['stop_pct'])
                if new_stop is not None and (pos['stop_price'] is None or new_stop > pos['stop_price']):
                    update_position(pos['id'], stop_price=new_stop)
                    mark_update_executed(u['id'], 'executed', f'stop -> {new_stop}')
                    self._event(f"STOP {pos['contract_symbol']} -> {new_stop}")
                else:
                    mark_update_executed(u['id'], 'noop', 'stop not tighter than current')
                continue

            if action == 'add':
                mark_update_executed(u['id'], 'skipped', 'add not supported'); continue

            if get_working_orders(pos['id'], 'sell'):
                continue  # a sell is already in flight; revisit next tick

            if action == 'trim':
                n = contracts_for_trim(pos['remaining_qty'], u['fraction'], u['contracts'])
                if n == 0:
                    mark_update_executed(u['id'], 'noop', 'runner rule: nothing to trim'); continue
                if self._sell(pos, n, 'trim', u['id']):
                    be_stop = round(pos['avg_cost'], 2)
                    update_position(pos['id'], trimmed=1,
                                    stop_price=max(pos['stop_price'] or 0, be_stop))
                    mark_update_executed(u['id'], 'executed', f'sell {n}')
                continue

            if action == 'close':
                if self._sell(pos, pos['remaining_qty'], 'close', u['id']):
                    mark_update_executed(u['id'], 'executed', f"sell {pos['remaining_qty']}")
                continue

    # ------------------------------------------------------------------
    # 4. positions
    # ------------------------------------------------------------------
    def _poll_positions(self):
        now = self._now()
        for pos in get_positions(('open',)):
            if pos['remaining_qty'] <= 0 or get_working_orders(pos['id'], 'sell'):
                continue
            reason = None
            if self.rules.time_stop_reached(pos['expiration'], now):
                reason = 'time_stop'
            else:
                quote = self._quote_for(pos)
                if quote is None:
                    continue
                if pos['stop_price'] is not None and quote.bid <= pos['stop_price']:
                    reason = 'runner_breakeven' if (pos['trimmed'] and
                                                    abs(pos['stop_price'] - pos['avg_cost']) < 1e-9) else 'stop'
            if reason:
                update_position(pos['id'], exit_reason=reason)
                self._sell(pos, pos['remaining_qty'], reason)

    def _quote_for(self, pos):
        contract = self.broker.resolve_contract(pos['ticker'], pos['option_type'],
                                                pos['strike'], pos['expiration'])
        return self.broker.get_quote(contract) if contract else None

    def _sell(self, pos, qty, reason, update_id=None) -> bool:
        if qty <= 0:
            return False
        contract = self.broker.resolve_contract(pos['ticker'], pos['option_type'],
                                                pos['strike'], pos['expiration'])
        quote = self.broker.get_quote(contract) if contract else None
        if quote is None:
            self._event(f"ERROR sell {pos['contract_symbol']}: no quote"); return False
        limit = max(quote.bid, 0.01)
        coid = f"sb-{pos['id']}-{reason}-{uuid.uuid4().hex[:8]}"
        oid = insert_order(pos['id'], self.broker.name, 'sell', reason, qty, limit,
                           update_id=update_id, client_order_id=coid,
                           submitted_at=self._now().isoformat())
        try:
            res = self.broker.sell_to_close(contract, qty, limit, client_order_id=coid)
        except Exception as exc:  # noqa: BLE001
            update_order(oid, status='error', error=str(exc)[:500])
            self._event(f"ERROR sell {pos['contract_symbol']}: {exc}"); return False
        update_order(oid, broker_order_id=res.broker_order_id, status=res.status)
        self._event(f"SELL {qty} x {pos['contract_symbol']} @ {limit} ({reason}) -> {res.status}")
        self._apply_order_state(get_order(oid), res)
        return True

    # ------------------------------------------------------------------
    # 5. daily loss / kill switch
    # ------------------------------------------------------------------
    def _check_daily_loss(self):
        day_start = self._now().astimezone(timezone.utc).strftime('%Y-%m-%dT00:00:00')
        realized = get_realized_pnl_since(day_start)
        unrealized = 0.0
        for pos in get_positions(('open',)):
            q = self._quote_for(pos)
            if q and pos['avg_cost'] is not None:
                unrealized += (q.bid - pos['avg_cost']) * 100 * pos['remaining_qty']
        total = realized + unrealized
        if total <= -abs(self.rules.max_daily_loss):
            self._halt(f'daily loss {total:+.2f} <= -{self.rules.max_daily_loss:g}')

    def _halt(self, why):
        self.halted = True
        self._event(f'HALT: {why} - cancelling orders and flattening')
        for w in get_working_orders():
            self._cancel(w, 'halt')
        for pos in get_positions(('open',)):
            if pos['remaining_qty'] > 0:
                update_position(pos['id'], exit_reason='halt')
                self._sell(pos, pos['remaining_qty'], 'halt')
        for pos in get_positions(('opening',)):
            update_position(pos['id'], status='missed', exit_reason='halt')
            update_signal_trade_status(pos['signal_id'], 'missed')
        try:
            with open(self.rules.kill_switch_file, 'a', encoding='utf-8') as fh:
                fh.write(f'{self._now().isoformat()} {why}\n')
        except OSError:
            log.exception('could not write kill switch file')


# ----------------------------------------------------------------------
def main():
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    from database import init_db
    from execution.alpaca import AlpacaPaperBroker

    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--interval', type=float, default=15.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    init_db()
    runner = Runner(AlpacaPaperBroker(), Rules.from_env())
    while True:
        for ev in runner.tick():
            print(ev)
        if args.once or runner.halted:
            break
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
