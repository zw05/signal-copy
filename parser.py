"""Text -> structured signal parsing. No database access here.

Two kinds of message are recognised:

* an ENTRY  -> ParsedSignal   ($SPY / $754 PUTS / EXPIRATION 9/16/2026 / $.68 Entry)
* an UPDATE -> ParsedUpdate   (price updates, trims, closes, stops, adds, notes)

Everything else is noise and returns None / [].
"""
import re
from dataclasses import dataclass
from datetime import datetime, date

PARSER_VERSION = 'mike_v2'

# "SOLD MAJORITY" with no explicit amount. Decided 2026-09-16.
DEFAULT_TRIM_FRACTION = 0.75
# Regex results below this go to needs_label instead of being stored.
MIN_CONFIDENCE = 0.8

# --------------------------------------------------------------------------
# entry patterns
# --------------------------------------------------------------------------

# "$SPY" - a ticker is letters only; "$754" (a strike) must not match.
TICKER_RE = re.compile(r'\$([A-Z]{1,5})(?![A-Za-z0-9.])')
STRIKE_TYPE_RE = re.compile(r'\$([\d,]+(?:\.\d+)?)\s+(CALL|PUT)S?\b', re.IGNORECASE)
EXPIRATION_RE = re.compile(r'EXPIRATION\s+(\d{1,2}/\d{1,2}/\d{2,4})', re.IGNORECASE)
ENTRY_RE = re.compile(r'\$?(\d*\.?\d+)\s+Entry\b', re.IGNORECASE)
AVG_RE = re.compile(r'\$?(\d*\.?\d+)\s+AVG\b', re.IGNORECASE)
# "$755 PT", "$755/$754 PT RANGE" -> first number
UNDERLYING_PT_RE = re.compile(r'\$(\d+(?:\.\d+)?)(?:/\$?\d+(?:\.\d+)?)?\s*PT\b', re.IGNORECASE)

# --------------------------------------------------------------------------
# update patterns (all matched against emoji-stripped, upper-cased text)
# --------------------------------------------------------------------------

PRICE_HERE_RE = re.compile(
    r'\$?(\d*\.\d+|\d+)\s+HERE\s+ON\s+(?:\$?([A-Z]{1,5})\s+)?(CALLS?|PUTS?)\b'
)
PCT_GAIN_RE = re.compile(r'\b(UP|DOWN)\s*([+-]?\d+(?:\.\d+)?)\s*%')
BARE_PRICE_RE = re.compile(r'(?<![\d.])\$(\d*\.\d+)(?![\d%])')

AMOUNT_WORDS = (
    r'(MAJORITY|MOST|SOME|A\s+FEW|HALF|A\s+THIRD|1/3|2/3|1/4|3/4|'
    r'\d{1,2}\s*%|\d{1,2}\s+CONTRACTS?)'
)
CLOSE_WORDS = r'(ALL|EVERYTHING|THE\s+REST|REST|REMAINING|RUNNERS?|LAST\s+ONES?|FULL)'

TRIM_RE = re.compile(
    r'\b(SOLD|SELLING|SELL|TRIMMED|TRIMMING|TRIM|TOOK|TAKING|TAKE|SCALED|SCALING)\s+'
    + AMOUNT_WORDS + r'(?:\s+(OFF|OUT|HERE|PROFITS?))?'
)
# "TAKING PROFITS" / "TRIMMING HERE" with no amount at all
TRIM_NO_AMOUNT_RE = re.compile(
    r'\b(TRIMMED|TRIMMING|TRIM|TAKING\s+(?:SOME\s+)?PROFITS?|TOOK\s+(?:SOME\s+)?PROFITS?|'
    r'SCALED\s+OUT|SCALING\s+OUT)\b'
)
# "TAKING 50% PROFIT" is ambiguous (sold 50%? up 50%?) -> low confidence
PCT_PROFIT_RE = re.compile(r'\b(TAKING|TOOK|TAKE)\s+(\d{1,2})\s*%\s+PROFITS?\b')
RUNNERS_LEFT_RE = re.compile(r'\b(LEAV(?:E|ING)|HOLDING|HOLD|KEEPING|KEEP)\s+(?:A\s+|SOME\s+|THE\s+)?RUNNERS?\b')

CLOSE_RE = re.compile(
    r'\b(SOLD|SELLING|SELL|CLOSED|CLOSING|CLOSE|OUT\s+OF)\s+' + CLOSE_WORDS + r'\b'
)
OUT_RE = re.compile(
    r'(?:^|\n)\s*(?:ALL\s+|FULLY\s+|COMPLETELY\s+)?OUT\b(?!\s+OF\s+(?:THE\s+)?(?:MONEY|OFFICE|TOWN))'
    r'|\b(ALL|FULLY|COMPLETELY)\s+OUT\b'
    r'|\bOUT\s+(HERE|NOW|OF\s+(?:SPY|QQQ|THESE|THIS|THEM|IT))\b'
    r'|\bRUNNERS?\s+(OUT|CLOSED|SOLD|GONE|DONE)\b'
    r'|\bSTOPPED\s+OUT\b'
    r'|\bCLOSED\s+(?:THE\s+)?(TRADE|POSITION)\b'
)

STOP_RE = re.compile(
    r'\b(STOP\s*LOSS|STOP|SL)\s*(?:IS|AT|@|:|SET\s+AT|SET\s+TO|TO)?\s*\$?(\d*\.\d+|\d+)(\s*%)?'
)
ADD_RE = re.compile(
    r"\b(ADDED|ADDING|ADD|DCA'?D|DCA'?ING|DCA|AVERAGED\s+DOWN|AVERAGING\s+DOWN)\b"
    r'(?:\s+(?:MORE\s+)?(?:AT|@|HERE\s+AT)?\s*\$?(\d*\.\d+))?'
)

TRADE_WORDS_RE = re.compile(
    r'\b(SOLD|SELL|SELLING|OUT|TRIM|TRIMMED|TRIMMING|STOP|SL|RUNNERS?|PROFITS?|'
    r'ADD|ADDED|ADDING|DCA|CALLS?|PUTS?|ENTRY|PT|CLOSED?|CLOSING|HOLD|HOLDING|'
    r'SCALED|SCALING|CUT|CUTTING|EXIT|EXITED|EXITING|TOOK|TAKING|LOSE|LOSING)\b'
)

# Characters we keep before matching update patterns. Everything else (emoji,
# pictographs, odd punctuation) becomes a space.
_KEEP_RE = re.compile(r'[^\w\s$.,/%+@:#()\'"-]', re.UNICODE)


@dataclass
class ParsedSignal:
    ticker: str
    option_type: str
    strike: float
    expiration: str | None
    action: str
    limit_price: float | None
    raw_excerpt: str
    confidence: float
    avg_price: float | None = None
    underlying_pt: float | None = None
    expiration_inferred: bool = False


@dataclass
class ParsedUpdate:
    action: str                      # price_update | trim | close | stop | add | note
    confidence: float
    raw_excerpt: str
    fraction: float | None = None
    contracts: int | None = None
    price: float | None = None
    pct_gain: float | None = None
    stop_price: float | None = None
    stop_pct: float | None = None
    ticker: str | None = None
    option_type: str | None = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def strip_emoji(text: str) -> str:
    return _KEEP_RE.sub(' ', text)


def _normalize_expiration(raw: str) -> str:
    for fmt in ('%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(raw, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return raw


def _normalize_price(raw: str) -> float:
    value = raw.replace(',', '')
    if value.startswith('.'):
        value = f'0{value}'
    return float(value)


def infer_expiration(signal_date: date) -> str:
    """No EXPIRATION line -> assume same-day (0DTE); roll weekends to Monday."""
    d = signal_date
    while d.weekday() >= 5:
        d = date.fromordinal(d.toordinal() + 1)
    return d.isoformat()


def _amount_to_fraction(amount: str) -> tuple[float | None, int | None]:
    a = re.sub(r'\s+', ' ', amount.strip().upper())
    if a in ('MAJORITY', 'MOST'):
        return DEFAULT_TRIM_FRACTION, None
    if a in ('SOME', 'A FEW'):
        return DEFAULT_TRIM_FRACTION, None
    if a in ('HALF',):
        return 0.5, None
    if a in ('A THIRD', '1/3'):
        return 1 / 3, None
    if a == '2/3':
        return 2 / 3, None
    if a == '1/4':
        return 0.25, None
    if a == '3/4':
        return 0.75, None
    m = re.match(r'(\d{1,2})\s*%', a)
    if m:
        return int(m.group(1)) / 100, None
    m = re.match(r'(\d{1,2}) CONTRACTS?', a)
    if m:
        return None, int(m.group(1))
    return DEFAULT_TRIM_FRACTION, None


# --------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------

def _score_signal(ticker, strike_match, expiration_match, entry_match) -> float:
    score = 0.0
    if ticker:
        score += 0.25
    if strike_match:
        score += 0.35
    if expiration_match:
        score += 0.25
    if entry_match:
        score += 0.15
    return round(score, 2)


def parse_option_message(content: str, signal_date: date | None = None) -> list[ParsedSignal]:
    ticker_match = TICKER_RE.search(content)
    strike_match = STRIKE_TYPE_RE.search(content)
    expiration_match = EXPIRATION_RE.search(content)
    entry_match = ENTRY_RE.search(content)
    avg_match = AVG_RE.search(content)
    pt_match = UNDERLYING_PT_RE.search(content)

    # An entry needs a ticker + strike/type and either an explicit entry price
    # or an expiration line. "$.99 HERE ON SPY PUTS" has neither -> not an entry.
    if not ticker_match or not strike_match:
        return []
    if not entry_match and not expiration_match:
        return []

    ticker = ticker_match.group(1).upper()
    strike = _normalize_price(strike_match.group(1))
    option_type = strike_match.group(2).lower()

    expiration_inferred = False
    if expiration_match:
        expiration = _normalize_expiration(expiration_match.group(1))
    elif signal_date is not None:
        expiration = infer_expiration(signal_date)
        expiration_inferred = True
    else:
        expiration = None

    limit_price = _normalize_price(entry_match.group(1)) if entry_match else None
    avg_price = _normalize_price(avg_match.group(1)) if avg_match else None
    underlying_pt = float(pt_match.group(1)) if pt_match else None
    confidence = _score_signal(ticker_match, strike_match, expiration_match, entry_match)

    excerpt_parts = [f'${ticker}', strike_match.group(0)]
    if expiration_match:
        excerpt_parts.append(expiration_match.group(0))
    if entry_match:
        excerpt_parts.append(entry_match.group(0))

    return [ParsedSignal(
        ticker=ticker,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        action='buy' if entry_match else 'unknown',
        limit_price=limit_price,
        raw_excerpt=' | '.join(excerpt_parts),
        confidence=confidence,
        avg_price=avg_price,
        underlying_pt=underlying_pt,
        expiration_inferred=expiration_inferred,
    )]


# --------------------------------------------------------------------------
# updates
# --------------------------------------------------------------------------

def looks_trade_related(content: str) -> bool:
    """Cheap gate: does this message plausibly talk about a position?"""
    text = strip_emoji(content).upper()
    if TRADE_WORDS_RE.search(text):
        return True
    if PCT_GAIN_RE.search(text) or BARE_PRICE_RE.search(text):
        return True
    return False


def parse_update_message(content: str) -> ParsedUpdate | None:
    """Return the primary action in a follow-up message, or None if nothing
    in the message maps to the vocabulary. Priority when several appear:
    close > trim > stop > add > price_update."""
    text = strip_emoji(content).upper()
    if not text.strip():
        return None

    # Components that can ride along with any primary action.
    price = None
    pct_gain = None
    ticker = None
    option_type = None
    excerpt = []

    m = PRICE_HERE_RE.search(text)
    if m:
        price = _normalize_price(m.group(1))
        ticker = m.group(2)
        option_type = 'call' if m.group(3).startswith('CALL') else 'put'
        excerpt.append(m.group(0))
    m = PCT_GAIN_RE.search(text)
    if m:
        pct = float(m.group(2).lstrip('+'))
        pct_gain = -abs(pct) if m.group(1) == 'DOWN' else pct
        excerpt.append(m.group(0))

    # ---- close --------------------------------------------------------
    m = CLOSE_RE.search(text) or OUT_RE.search(text)
    if m:
        if price is None:
            bp = BARE_PRICE_RE.search(text)
            price = _normalize_price(bp.group(1)) if bp else None
        excerpt.insert(0, m.group(0).strip())
        return ParsedUpdate(
            action='close', confidence=0.9, raw_excerpt=' | '.join(excerpt),
            price=price, pct_gain=pct_gain, ticker=ticker, option_type=option_type,
        )

    # ---- trim ---------------------------------------------------------
    m = PCT_PROFIT_RE.search(text)
    if m:
        # "TAKING 50% PROFIT": sold 50%? or up 50%? Ambiguous -> label queue.
        excerpt.insert(0, m.group(0))
        return ParsedUpdate(
            action='trim', confidence=0.6, raw_excerpt=' | '.join(excerpt),
            fraction=int(m.group(2)) / 100, price=price, pct_gain=pct_gain,
            ticker=ticker, option_type=option_type,
        )
    m = TRIM_RE.search(text)
    if m:
        fraction, contracts = _amount_to_fraction(m.group(2))
        if price is None:
            bp = BARE_PRICE_RE.search(text)
            price = _normalize_price(bp.group(1)) if bp else None
        excerpt.insert(0, m.group(0))
        return ParsedUpdate(
            action='trim', confidence=0.95, raw_excerpt=' | '.join(excerpt),
            fraction=fraction, contracts=contracts, price=price, pct_gain=pct_gain,
            ticker=ticker, option_type=option_type,
        )
    m = TRIM_NO_AMOUNT_RE.search(text) or RUNNERS_LEFT_RE.search(text)
    if m:
        excerpt.insert(0, m.group(0))
        return ParsedUpdate(
            action='trim', confidence=0.85, raw_excerpt=' | '.join(excerpt),
            fraction=DEFAULT_TRIM_FRACTION, price=price, pct_gain=pct_gain,
            ticker=ticker, option_type=option_type,
        )

    # ---- stop ---------------------------------------------------------
    m = STOP_RE.search(text)
    if m:
        value = _normalize_price(m.group(2))
        is_pct = bool(m.group(3))
        excerpt.insert(0, m.group(0))
        return ParsedUpdate(
            action='stop', confidence=0.9, raw_excerpt=' | '.join(excerpt),
            stop_price=None if is_pct else value, stop_pct=value if is_pct else None,
            price=price, pct_gain=pct_gain, ticker=ticker, option_type=option_type,
        )

    # ---- add ----------------------------------------------------------
    m = ADD_RE.search(text)
    if m:
        add_price = _normalize_price(m.group(2)) if m.group(2) else price
        excerpt.insert(0, m.group(0))
        return ParsedUpdate(
            action='add', confidence=0.85, raw_excerpt=' | '.join(excerpt),
            price=add_price, pct_gain=pct_gain, ticker=ticker, option_type=option_type,
        )

    # ---- price update -------------------------------------------------
    if price is not None or pct_gain is not None:
        return ParsedUpdate(
            action='price_update', confidence=0.95, raw_excerpt=' | '.join(excerpt),
            price=price, pct_gain=pct_gain, ticker=ticker, option_type=option_type,
        )

    return None


if __name__ == '__main__':
    SAMPLE = """$SPY
$754 PUTS
 EXPIRATION 9/16/2026
$.68 Entry
@everyone

seeking for these to go ITM with a $755 PT."""
    for signal in parse_option_message(SAMPLE):
        print(signal)
    for text in ('$.99 HERE ON SPY PUTS\nUP +48% 🔥\n@everyone',
                 'SOLD MAJORITY 🚨🚨🚨',
                 'NAILED THAT SELLOFF!!! 🎯🎯'):
        print(repr(text[:30]), '->', parse_update_message(text))
