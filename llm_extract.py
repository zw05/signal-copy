"""LLM fallback for follow-up messages the regex parser can't map.

This is *parsing*, not decision-making: the model is asked what the human
said, never whether to trade. Disabled (returns None) when ANTHROPIC_API_KEY
is not set so the rest of the pipeline works offline.
"""
import json
import logging
import os

from parser import ParsedUpdate, strip_emoji

log = logging.getLogger(__name__)

EXTRACT_MODEL = os.getenv('EXTRACT_MODEL', 'claude-haiku-4-5')

SYSTEM = """You turn a Discord message from an options trader into one structured action.
The trader posts an entry (e.g. "$SPY $754 PUTS $.68 Entry") and then follow-ups about
that same position. Classify ONLY what the follow-up literally says:

- price_update: a current price and/or "UP +48%" style status. This is NOT a sale.
- trim: sold PART of the position ("sold majority", "took half off", "leaving runners").
  fraction = share of the position sold (majority/most/some = 0.75, half = 0.5).
- close: sold ALL remaining contracts ("out", "all out", "runners out", "closed").
- stop: a stop-loss level was stated (stop_price in option dollars, or stop_pct).
- add: bought more contracts ("added at .28", "DCA'd").
- note: commentary about the trade with no action (targets, "be patient", "nailed it").
- none: not about a position at all.

"UP +X%" or "taking X% profit" means the trade is up X% - it is NOT a fraction sold
unless the message also says an amount was sold. If unsure, lower the confidence."""

SCHEMA = {
    'type': 'object',
    'properties': {
        'action': {'type': 'string',
                   'enum': ['price_update', 'trim', 'close', 'stop', 'add', 'note', 'none']},
        'fraction': {'type': ['number', 'null']},
        'contracts': {'type': ['integer', 'null']},
        'price': {'type': ['number', 'null']},
        'pct_gain': {'type': ['number', 'null']},
        'stop_price': {'type': ['number', 'null']},
        'stop_pct': {'type': ['number', 'null']},
        'ticker': {'type': ['string', 'null']},
        'option_type': {'type': ['string', 'null'], 'enum': ['call', 'put', None]},
        'confidence': {'type': 'number'},
    },
    'required': ['action', 'fraction', 'contracts', 'price', 'pct_gain',
                 'stop_price', 'stop_pct', 'ticker', 'option_type', 'confidence'],
    'additionalProperties': False,
}

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not os.getenv('ANTHROPIC_API_KEY'):
        return None
    try:
        import anthropic
    except ImportError:
        log.warning('anthropic SDK not installed; LLM extraction disabled')
        return None
    _client = anthropic.Anthropic()
    return _client


def extract_update(content: str, open_signal_summaries: list[str]) -> ParsedUpdate | None:
    client = _get_client()
    if client is None:
        return None

    context = '\n'.join(f'- {s}' for s in open_signal_summaries) or '- (none)'
    user = (
        f'Open positions this trader currently has:\n{context}\n\n'
        f'Follow-up message:\n"""\n{strip_emoji(content).strip()}\n"""'
    )
    try:
        response = client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=256,
            system=SYSTEM,
            messages=[{'role': 'user', 'content': user}],
            output_config={'format': {'type': 'json_schema', 'schema': SCHEMA}},
        )
    except Exception:  # noqa: BLE001 - never let the LLM path crash ingestion
        log.exception('LLM extraction failed')
        return None

    if response.stop_reason == 'refusal':
        return None
    text = next((b.text for b in response.content if b.type == 'text'), None)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning('LLM extraction returned non-JSON: %r', text[:200])
        return None

    if data['action'] == 'none':
        return None
    return ParsedUpdate(
        action=data['action'],
        confidence=float(data['confidence']),
        raw_excerpt=f'llm:{EXTRACT_MODEL}',
        fraction=data['fraction'],
        contracts=data['contracts'],
        price=data['price'],
        pct_gain=data['pct_gain'],
        stop_price=data['stop_price'],
        stop_pct=data['stop_pct'],
        ticker=(data['ticker'] or '').upper() or None,
        option_type=data['option_type'],
    )
