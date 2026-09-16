from datetime import date

import pytest

from parser import (
    DEFAULT_TRIM_FRACTION,
    looks_trade_related,
    parse_option_message,
    parse_update_message,
)

# Real message shapes from the channel (2026-09-16, SPY 754P 0DTE).
ENTRY = """$SPY
$754 PUTS
 EXPIRATION 9/16/2026
$.68 Entry
@everyone

seeking for these to go ITM with a $755 PT.

15M chart rejection and ..."""

ENTRY_WITH_AVG = """$SPY
$742 CALLS
 EXPIRATION 6/23/2026
$.45 Entry, $.42 AVG
@everyone

looking for a break above $736.4 to then see $740 & finally $742

take your time & leave DCA room"""

ENTRY_NO_EXP = """$SPY
$742 CALLS
$.45 Entry"""

PRICE_UPDATE_1 = "$.99 HERE ON SPY PUTS\nUP +48% 🔥\n@everyone"
PRICE_UPDATE_2 = "$1.25 HERE ON SPY PUTS\nUP +85% 🔥\n@everyone"
PRICE_UPDATE_3 = "$1.36 HERE ON SPY PUTS\nUP +100% ☢️\n@everyone"
SOLD_MAJORITY = "SOLD MAJORITY 🚨🚨🚨"
NOTE_1 = "NAILED THAT SELLOFF!!! 🎯🎯\n@everyone"
NOTE_2 = "$755/$754 PT RANGE HITTING 🎯🎯\n@everyone"

NOISE = [
    "How am I gonna get a higher percentage?",
    "for now i guess keep them on, but later on when you have a higher % id equip shiny mythics if you can",
    "563%",
    "whats your hs with it?",
    "check out this chart",
    "watch out for the fed at 2",
]


# --------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------

def test_entry_full():
    (s,) = parse_option_message(ENTRY, signal_date=date(2026, 9, 16))
    assert s.ticker == 'SPY'
    assert s.option_type == 'put'
    assert s.strike == 754.0
    assert s.expiration == '2026-09-16'
    assert s.expiration_inferred is False
    assert s.limit_price == 0.68
    assert s.underlying_pt == 755.0
    assert s.action == 'buy'
    assert s.confidence == 1.0


def test_entry_with_avg():
    (s,) = parse_option_message(ENTRY_WITH_AVG)
    assert s.option_type == 'call'
    assert s.limit_price == 0.45
    assert s.avg_price == 0.42
    assert s.expiration == '2026-06-23'
    # "$736.4 ... $740 & finally $742" must not be read as a PT
    assert s.underlying_pt is None


def test_entry_missing_expiration_is_inferred_0dte():
    (s,) = parse_option_message(ENTRY_NO_EXP, signal_date=date(2026, 9, 16))
    assert s.expiration == '2026-09-16'
    assert s.expiration_inferred is True


def test_entry_missing_expiration_rolls_weekend():
    (s,) = parse_option_message(ENTRY_NO_EXP, signal_date=date(2026, 9, 19))  # Saturday
    assert s.expiration == '2026-09-21'


def test_entry_flattened_one_line():
    # Discord's reply-preview / copy-paste collapses newlines.
    flat = "$SPY $754 PUTS  EXPIRATION 9/16/2026 $.68 Entry @everyone • seeking for these to go ITM with a $755 PT."
    (s,) = parse_option_message(flat)
    assert (s.ticker, s.strike, s.option_type, s.limit_price) == ('SPY', 754.0, 'put', 0.68)


@pytest.mark.parametrize('text', [PRICE_UPDATE_1, SOLD_MAJORITY, NOTE_1, NOTE_2] + NOISE)
def test_non_entries_are_not_entries(text):
    assert parse_option_message(text) == []


# --------------------------------------------------------------------------
# updates
# --------------------------------------------------------------------------

@pytest.mark.parametrize('text,price,pct', [
    (PRICE_UPDATE_1, 0.99, 48.0),
    (PRICE_UPDATE_2, 1.25, 85.0),
    (PRICE_UPDATE_3, 1.36, 100.0),
])
def test_price_update_is_not_a_sale(text, price, pct):
    u = parse_update_message(text)
    assert u.action == 'price_update'
    assert u.price == price
    assert u.pct_gain == pct
    assert u.ticker == 'SPY'
    assert u.option_type == 'put'
    assert u.fraction is None


def test_price_update_down():
    u = parse_update_message("$.40 HERE ON SPY CALLS\nDOWN -12%")
    assert u.action == 'price_update'
    assert u.pct_gain == -12.0
    assert u.option_type == 'call'


def test_sold_majority():
    u = parse_update_message(SOLD_MAJORITY)
    assert u.action == 'trim'
    assert u.fraction == DEFAULT_TRIM_FRACTION
    assert u.confidence >= 0.8


@pytest.mark.parametrize('text,fraction', [
    ('sold half here', 0.5),
    ('SOLD 1/3 🔥', 1 / 3),
    ('took 2/3 off at $.90', 2 / 3),
    ('trimmed 50% here', 0.5),
    ('SELLING MOST, leaving runners', DEFAULT_TRIM_FRACTION),
    ('scaled out some', DEFAULT_TRIM_FRACTION),
    ('taking profits here 🔥', DEFAULT_TRIM_FRACTION),
    ('leaving a runner', DEFAULT_TRIM_FRACTION),
])
def test_trim_fractions(text, fraction):
    u = parse_update_message(text)
    assert u.action == 'trim'
    assert u.fraction == pytest.approx(fraction)


def test_trim_with_price():
    u = parse_update_message('took 2/3 off at $.90')
    assert u.price == 0.90


def test_trim_contracts():
    u = parse_update_message('sold 2 contracts')
    assert u.action == 'trim'
    assert u.contracts == 2
    assert u.fraction is None


def test_taking_pct_profit_is_low_confidence():
    # Ambiguous: sold 50%? up 50%? Must not be auto-executed.
    u = parse_update_message('taking 50% profit here')
    assert u.action == 'trim'
    assert u.confidence < 0.8


@pytest.mark.parametrize('text', [
    'OUT',
    'all out 🔥',
    'ALL OUT HERE',
    'sold the rest',
    'SOLD ALL',
    'runners out',
    'closed the rest at $1.40',
    'stopped out',
    'closed the trade',
    'out of these here',
    'fully out',
])
def test_close_phrasings(text):
    u = parse_update_message(text)
    assert u.action == 'close', text


def test_close_captures_price():
    u = parse_update_message('closed the rest at $1.40')
    assert u.price == 1.40


def test_close_beats_trim_when_both_present():
    u = parse_update_message('sold half earlier, now all out')
    assert u.action == 'close'


@pytest.mark.parametrize('text,stop_price,stop_pct', [
    ('stop at .25', 0.25, None),
    ('SL .30', 0.30, None),
    ('stop loss set at $0.5', 0.5, None),
    ('stop 20%', None, 20.0),
])
def test_stop(text, stop_price, stop_pct):
    u = parse_update_message(text)
    assert u.action == 'stop'
    assert u.stop_price == stop_price
    assert u.stop_pct == stop_pct


@pytest.mark.parametrize('text,price', [
    ('added at .28', 0.28),
    ("DCA'd here at $.30", 0.30),
    ('adding more', None),
])
def test_add(text, price):
    u = parse_update_message(text)
    assert u.action == 'add'
    assert u.price == price


@pytest.mark.parametrize('text', [NOTE_1, NOTE_2] + NOISE)
def test_notes_and_noise_have_no_action(text):
    assert parse_update_message(text) is None, text


def test_selloff_does_not_trigger_sell():
    # "SELLOFF" contains "SELL" - word boundaries must hold.
    assert parse_update_message('NAILED THAT SELLOFF!!!') is None


def test_looks_trade_related():
    assert looks_trade_related('cut these at .20')
    assert looks_trade_related('$.99 here')
    assert not looks_trade_related('good morning everyone')
