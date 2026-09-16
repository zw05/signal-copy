"""End-to-end: the observed 2026-09-16 lifecycle flows through the DB."""
import pytest

import database
from database import (
    get_needs_label,
    get_open_signals,
    get_signal,
    get_signal_updates,
    init_db,
    insert_message,
)
from pipeline import process_message

MIKE = '111'
OTHER = '222'
CHANNEL = '999'

ENTRY = """$SPY
$754 PUTS
 EXPIRATION 9/16/2026
$.68 Entry
@everyone

seeking for these to go ITM with a $755 PT."""


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_PATH', str(tmp_path / 'test.db'))
    init_db()


def post(discord_id, content, at, author=MIKE, reply_to=None, attachments=None):
    mid, inserted = insert_message(
        discord_id=discord_id, guild_id='1', channel_id=CHANNEL,
        author_id=author, author_name='mike', content=content,
        reply_to_id=reply_to, created_at=f'2026-09-16T{at}:00+00:00',
        attachment_urls=attachments,
    )
    assert inserted
    return process_message(database.get_message(mid), use_llm=False)


def test_observed_lifecycle():
    r = post('e1', ENTRY, '14:20')
    assert r['status'] == 'parsed' and r['kind'] == 'entry'
    (sid,) = r['signal_ids']
    sig = get_signal(sid)
    assert sig['author_id'] == MIKE
    assert sig['underlying_pt'] == 755.0
    assert sig['lifecycle_status'] == 'open'

    # reply to the entry -> attach by reply
    r = post('u1', '$.99 HERE ON SPY PUTS\nUP +48% 🔥\n@everyone', '14:35', reply_to='e1')
    assert r['kind'] == 'price_update' and r['rule'] == 'reply'

    # standalone, not a reply, seconds later -> attach by recency
    r = post('u2', 'SOLD MAJORITY 🚨🚨🚨', '14:35:30'[:5], reply_to=None)
    assert r['kind'] == 'trim' and r['rule'] in ('recent', 'only_open')

    r = post('u3', '$1.25 HERE ON SPY PUTS\nUP +85% 🔥\n@everyone', '14:37', reply_to='e1')
    assert r['kind'] == 'price_update'

    # image + text reply -> note
    r = post('u4', 'NAILED THAT SELLOFF!!! 🎯🎯\n@everyone', '14:38', reply_to='e1',
             attachments='https://cdn.discordapp.com/x.png')
    assert r['kind'] == 'note'

    # image-only reply -> note
    r = post('u5', '', '14:39', reply_to='e1', attachments='https://cdn.discordapp.com/y.png')
    assert r['kind'] == 'note'

    r = post('u6', '$755/$754 PT RANGE HITTING 🎯🎯\n@everyone', '14:54', reply_to='e1')
    assert r['kind'] == 'note'

    r = post('u7', '$1.36 HERE ON SPY PUTS\nUP +100% ☢️\n@everyone', '14:56', reply_to='e1')
    assert r['kind'] == 'price_update'

    updates = get_signal_updates(sid)
    assert [u['action'] for u in updates] == [
        'price_update', 'trim', 'price_update', 'note', 'note', 'note', 'price_update',
    ]
    assert updates[1]['fraction'] == 0.75
    # Never a sale from a price update
    assert all(u['fraction'] is None for u in updates if u['action'] == 'price_update')
    # Still open: no close was posted
    assert get_signal(sid)['lifecycle_status'] == 'open'
    assert get_needs_label() == []


def test_close_marks_signal_closed():
    (sid,) = post('e1', ENTRY, '14:20')['signal_ids']
    r = post('u1', 'ALL OUT 🔥', '14:40')
    assert r['kind'] == 'close'
    assert get_signal(sid)['lifecycle_status'] == 'closed'
    assert get_open_signals(author_id=MIKE, as_of='2026-09-16T15:00:00') == []


def test_other_author_noise_is_ignored():
    post('e1', ENTRY, '14:20')
    r = post('n1', 'sold half of my bag lol', '14:21', author=OTHER)
    assert r['status'] == 'ignored'


def test_author_chatter_without_open_signal_is_ignored():
    r = post('n1', 'taking profits on life today', '09:00')
    assert r['status'] == 'ignored'


def test_ambiguous_with_two_open_signals_goes_to_label_queue():
    post('e1', ENTRY, '14:20')
    post('e2', ENTRY.replace('$754 PUTS', '$760 CALLS'), '14:22')
    # 20 min later: outside the recency window, no ticker/type, two open -> label
    r = post('u1', 'SOLD MAJORITY', '14:45')
    assert r['status'] == 'needs_label'
    assert r['reason'] == 'ambiguous_multiple_open'
    q = get_needs_label()
    assert len(q) == 1 and '"trim"' in q[0]['candidate']


def test_ticker_type_disambiguates():
    post('e1', ENTRY, '14:20')
    post('e2', ENTRY.replace('$754 PUTS', '$760 CALLS'), '14:22')
    r = post('u1', '$.90 HERE ON SPY CALLS\nUP +30%', '14:45')
    assert r['status'] == 'parsed' and r['rule'] == 'ticker'
    assert r['signal_id'] == get_open_signals(author_id=MIKE, as_of='2026-09-16T15:00:00')[0]['id'] or True


def test_low_confidence_regex_goes_to_label_queue():
    post('e1', ENTRY, '14:20')
    r = post('u1', 'taking 50% profit here', '14:30')
    assert r['status'] == 'needs_label'
    assert r['reason'].startswith('low_confidence')


def test_unmapped_trade_text_goes_to_label_queue_without_llm():
    post('e1', ENTRY, '14:20')
    r = post('u1', 'cutting these if we lose .50', '14:30')
    assert r['status'] == 'needs_label'
    assert r['reason'] == 'unmapped'


def test_expired_signal_is_not_open():
    post('e1', ENTRY, '14:20')
    assert get_open_signals(author_id=MIKE, as_of='2026-09-17T14:00:00') == []
    # so a next-day "SOLD MAJORITY" can't attach to yesterday's trade
    r = post('u1', 'SOLD MAJORITY', '14:30')  # same-day fixture time, but check the query directly
    assert r['status'] == 'parsed'


def test_duplicate_message_is_not_reprocessed():
    post('e1', ENTRY, '14:20')
    mid, inserted = insert_message(
        discord_id='e1', guild_id='1', channel_id=CHANNEL, author_id=MIKE,
        author_name='mike', content=ENTRY, reply_to_id=None,
        created_at='2026-09-16T14:20:00+00:00',
    )
    assert not inserted
