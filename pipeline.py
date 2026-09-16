"""Run one stored message through the parser and persist the result.

Outcomes (also written to messages.parse_status):
  parsed       - an entry or an update was stored
  needs_label  - looks like a trade message but couldn't be mapped safely
  ignored      - noise
"""
import json
import logging
from dataclasses import asdict
from datetime import datetime

from attach import find_signal_for_update
from database import (
    get_open_signals,
    insert_needs_label,
    insert_signal,
    insert_signal_update,
    update_message_parse_status,
    update_signal_lifecycle,
)
from llm_extract import extract_update
from parser import (
    MIN_CONFIDENCE,
    PARSER_VERSION,
    ParsedUpdate,
    looks_trade_related,
    parse_option_message,
    parse_update_message,
)

log = logging.getLogger(__name__)


def _signal_summary(s: dict) -> str:
    return (f"#{s['id']} {s['ticker']} {s['strike']:g} {s['option_type']} "
            f"exp {s['expiration']} entry {s['limit_price']}")


def _store_entries(message: dict, entries) -> list[int]:
    ids = []
    for e in entries:
        ids.append(insert_signal(
            message_id=message['id'],
            ticker=e.ticker,
            option_type=e.option_type,
            strike=e.strike,
            expiration=e.expiration,
            action=e.action,
            contracts=None,
            limit_price=e.limit_price,
            raw_excerpt=e.raw_excerpt,
            confidence=e.confidence,
            parser_version=PARSER_VERSION,
            avg_price=e.avg_price,
            underlying_pt=e.underlying_pt,
            expiration_inferred=int(e.expiration_inferred),
            author_id=str(message['author_id']),
            signal_created_at=message['created_at'],
        ))
    update_message_parse_status(message['id'], 'parsed')
    return ids


def _store_update(message: dict, signal_id: int, update: ParsedUpdate,
                  source: str, rule: str) -> int:
    update_id = insert_signal_update(
        signal_id=signal_id,
        message_id=message['id'],
        action=update.action,
        source=source,
        parser_version=PARSER_VERSION,
        fraction=update.fraction,
        contracts=update.contracts,
        price=update.price,
        pct_gain=update.pct_gain,
        stop_price=update.stop_price,
        stop_pct=update.stop_pct,
        confidence=update.confidence,
        attach_rule=rule,
        raw_excerpt=update.raw_excerpt,
    )
    if update.action == 'close':
        update_signal_lifecycle(signal_id, 'closed')
    update_message_parse_status(message['id'], 'parsed')
    return update_id


def _needs_label(message: dict, reason: str, update: ParsedUpdate | None = None) -> dict:
    candidate = json.dumps(asdict(update)) if update else None
    insert_needs_label(message['id'], reason, candidate)
    update_message_parse_status(message['id'], 'needs_label')
    return {'status': 'needs_label', 'reason': reason}


def _ignore(message: dict) -> dict:
    update_message_parse_status(message['id'], 'ignored')
    return {'status': 'ignored'}


def process_message(message: dict, use_llm: bool = True) -> dict:
    content = message.get('content') or ''
    has_attachments = bool(message.get('attachment_urls'))

    # ---- entry ------------------------------------------------------------
    try:
        signal_date = datetime.fromisoformat(message['created_at']).date()
    except ValueError:
        signal_date = None
    entries = parse_option_message(content, signal_date=signal_date)
    if entries:
        ids = _store_entries(message, entries)
        return {'status': 'parsed', 'kind': 'entry', 'signal_ids': ids}

    # ---- update -----------------------------------------------------------
    update = parse_update_message(content) if content.strip() else None
    signal_id, rule = find_signal_for_update(message, update)

    # Nothing open for this author (and not a reply to a signal): whatever the
    # text says, it isn't about a position we're tracking.
    if signal_id is None and rule == 'no_open_signal':
        return _ignore(message)

    if update is None:
        if rule == 'reply' and signal_id is not None:
            # Commentary (or an image) posted as a reply to the entry.
            note = ParsedUpdate(action='note', confidence=1.0,
                                raw_excerpt=(content[:120] or '[attachment]'))
            uid = _store_update(message, signal_id, note, 'regex', rule)
            return {'status': 'parsed', 'kind': 'note', 'update_id': uid}
        if not content.strip() or not looks_trade_related(content):
            return _ignore(message)

        # Trade-looking text from an author with an open position that regex
        # couldn't map: ask the model what was said.
        if use_llm:
            summaries = [_signal_summary(s) for s in
                         get_open_signals(author_id=str(message['author_id']),
                                          as_of=message['created_at'])]
            update = extract_update(content, summaries)
            source = 'llm'
            if update is not None:
                signal_id, rule = find_signal_for_update(message, update)
        if update is None:
            return _needs_label(message, 'unmapped')
    else:
        source = 'regex'

    if signal_id is None:
        return _needs_label(message, rule, update)
    if update.confidence < MIN_CONFIDENCE:
        return _needs_label(message, f'low_confidence:{source}', update)

    uid = _store_update(message, signal_id, update, source, rule)
    return {'status': 'parsed', 'kind': update.action, 'update_id': uid,
            'signal_id': signal_id, 'rule': rule, 'source': source}
