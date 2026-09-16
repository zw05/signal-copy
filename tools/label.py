"""Resolve the needs_label queue by hand.

    python -m tools.label            # interactive
    python -m tools.label --list     # just show the queue

For each message you pick an action and (optionally) a fraction/price, and
the signal it belongs to. The result is stored as a manual signal_update.
"""
import argparse
import sys

from database import (
    get_needs_label,
    get_open_signals,
    get_signal,
    init_db,
    insert_signal_update,
    resolve_needs_label,
    update_message_parse_status,
    update_signal_lifecycle,
)
from parser import PARSER_VERSION

ACTIONS = ['price_update', 'trim', 'close', 'stop', 'add', 'note', 'ignore']


def _ask(prompt, default=None):
    raw = input(f'{prompt}{f" [{default}]" if default is not None else ""}: ').strip()
    return raw if raw else default


def _ask_float(prompt):
    raw = _ask(prompt)
    return float(raw) if raw else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true')
    args = ap.parse_args()
    init_db()

    queue = get_needs_label()
    if not queue:
        print('needs_label queue is empty.')
        return

    for item in queue:
        print('\n' + '=' * 70)
        print(f"message {item['message_id']}  {item['message_created_at']}  "
              f"{item['author_name']}  reason={item['reason']}")
        print('-' * 70)
        print(item['content'])
        if item['candidate']:
            print(f"\nparser guess: {item['candidate']}")
        if args.list:
            continue

        signals = get_open_signals(author_id=None, as_of=item['message_created_at'])
        print('\nopen signals at that time:')
        for s in signals:
            print(f"  #{s['id']} {s['ticker']} {s['strike']:g} {s['option_type']} "
                  f"exp {s['expiration']} entry {s['limit_price']}")

        action = _ask(f'action {ACTIONS}', 'ignore')
        if action not in ACTIONS:
            print('unknown action, skipping'); continue
        if action == 'ignore':
            resolve_needs_label(item['message_id'])
            update_message_parse_status(item['message_id'], 'ignored')
            continue

        sid = _ask('signal id')
        if not sid or not get_signal(int(sid)):
            print('no such signal, skipping'); continue
        sid = int(sid)

        fraction = _ask_float('fraction sold (0-1)') if action == 'trim' else None
        price = _ask_float('price') if action in ('price_update', 'trim', 'close', 'add') else None
        stop_price = _ask_float('stop price') if action == 'stop' else None
        pct_gain = _ask_float('pct gain') if action == 'price_update' else None

        insert_signal_update(
            signal_id=sid, message_id=item['message_id'], action=action,
            source='manual', parser_version=PARSER_VERSION, fraction=fraction,
            price=price, stop_price=stop_price, pct_gain=pct_gain,
            confidence=1.0, attach_rule='manual', raw_excerpt=item['content'][:120],
        )
        if action == 'close':
            update_signal_lifecycle(sid, 'closed')
        resolve_needs_label(item['message_id'])
        update_message_parse_status(item['message_id'], 'parsed')
        print('saved.')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
