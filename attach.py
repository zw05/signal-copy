"""Decide which open signal a follow-up message belongs to.

Rules, in order (see PLAN.md section 2.2):
  1. reply       - message replies to the entry (or to another update of it)
  2. ticker      - message names the ticker/type and exactly one open signal matches
  3. recent      - standalone message from the author within RECENT_WINDOW of
                   the author's last entry/update -> same signal
  4. only_open   - the author has exactly one open signal
  5. (none)      - ambiguous -> caller sends it to needs_label
"""
from datetime import datetime, timedelta

from database import (
    get_last_activity,
    get_message_by_discord_id,
    get_open_signals,
    get_signal_by_message_id,
    get_update_by_message_id,
)

RECENT_WINDOW = timedelta(minutes=10)


def _parse_ts(value: str) -> datetime:
    # Discord timestamps are ISO with offset; strip tz for simple comparison.
    return datetime.fromisoformat(value).replace(tzinfo=None)


def _signal_from_reply(reply_to_discord_id: str):
    parent = get_message_by_discord_id(reply_to_discord_id)
    if not parent:
        return None
    signal = get_signal_by_message_id(parent['id'])
    if signal:
        return signal['id']
    update = get_update_by_message_id(parent['id'])
    if update:
        return update['signal_id']
    return None


def find_signal_for_update(message: dict, update) -> tuple[int | None, str]:
    """Return (signal_id, rule) or (None, reason)."""
    author_id = str(message['author_id'])
    as_of = message['created_at']

    # 1. reply chain
    if message.get('reply_to_id'):
        signal_id = _signal_from_reply(message['reply_to_id'])
        if signal_id is not None:
            return signal_id, 'reply'

    open_signals = get_open_signals(author_id=author_id, as_of=as_of)
    if not open_signals:
        return None, 'no_open_signal'

    # 2. ticker (+ type) named in the message
    if update is not None and update.ticker:
        matches = [
            s for s in open_signals
            if s['ticker'] == update.ticker
            and (update.option_type is None or s['option_type'] == update.option_type)
        ]
        if len(matches) == 1:
            return matches[0]['id'], 'ticker'
        if len(matches) > 1:
            return None, 'ambiguous_ticker'

    # 3. standalone message shortly after the author's last activity
    last = get_last_activity(author_id, before=as_of)
    if last:
        try:
            gap = _parse_ts(as_of) - _parse_ts(last['at'])
        except ValueError:
            gap = None
        if gap is not None and timedelta(0) <= gap <= RECENT_WINDOW:
            if any(s['id'] == last['signal_id'] for s in open_signals):
                return last['signal_id'], 'recent'

    # 4. only one thing it could be
    if len(open_signals) == 1:
        return open_signals[0]['id'], 'only_open'

    return None, 'ambiguous_multiple_open'
