"""Mechanical safety rules and sizing. Read once from the environment.

Nothing here makes a trading decision - it only says whether the copied
action is allowed and how big it is. See PLAN.md section 5.
"""
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Rules:
    contracts_per_entry: int = 3        # 3 lets "sold majority" (0.75) keep 1 runner
    max_open_positions: int = 3
    max_daily_loss: float = 500.0       # dollars; trips the kill switch
    min_signal_confidence: float = 0.75
    stale_signal_minutes: int = 3       # skip entries older than this when first seen
    entry_chase_pct: float = 10.0       # limit <= source entry * (1 + pct/100)
    entry_order_ttl_minutes: int = 5    # cancel unfilled entry after this
    sell_order_ttl_seconds: int = 60    # re-price unfilled sells after this
    hard_stop_pct: float = 50.0         # from our fill, if the source never posts a stop
    time_stop_et: str = '15:45'         # flatten anything expiring today at this ET time
    max_spread_pct: float = 25.0
    min_open_interest: int = 100
    kill_switch_file: str = 'KILL'

    @classmethod
    def from_env(cls):
        return cls(
            contracts_per_entry=_i('CONTRACTS_PER_ENTRY', cls.contracts_per_entry),
            max_open_positions=_i('MAX_OPEN_POSITIONS', cls.max_open_positions),
            max_daily_loss=_f('MAX_DAILY_LOSS', cls.max_daily_loss),
            min_signal_confidence=_f('MIN_SIGNAL_CONFIDENCE', cls.min_signal_confidence),
            stale_signal_minutes=_i('STALE_SIGNAL_MINUTES', cls.stale_signal_minutes),
            entry_chase_pct=_f('ENTRY_CHASE_PCT', cls.entry_chase_pct),
            entry_order_ttl_minutes=_i('ENTRY_ORDER_TTL_MINUTES', cls.entry_order_ttl_minutes),
            sell_order_ttl_seconds=_i('SELL_ORDER_TTL_SECONDS', cls.sell_order_ttl_seconds),
            hard_stop_pct=_f('HARD_STOP_PCT', cls.hard_stop_pct),
            time_stop_et=os.getenv('TIME_STOP_ET', cls.time_stop_et),
            max_spread_pct=_f('MAX_SPREAD_PCT', cls.max_spread_pct),
            min_open_interest=_i('MIN_OPEN_INTEREST', cls.min_open_interest),
            kill_switch_file=os.getenv('KILL_SWITCH_FILE', cls.kill_switch_file),
        )

    # ---- checks ----------------------------------------------------------

    def kill_switch_on(self) -> bool:
        return os.path.exists(self.kill_switch_file)

    def entry_limit(self, source_entry: float | None, ask: float) -> float:
        """Buy at the ask, but never more than the source's entry + chase."""
        if source_entry is None:
            return ask
        cap = round(source_entry * (1 + self.entry_chase_pct / 100), 2)
        return min(ask, cap)

    def entry_too_expensive(self, source_entry: float | None, ask: float) -> bool:
        if source_entry is None:
            return False
        return ask > source_entry * (1 + self.entry_chase_pct / 100)

    def is_stale(self, signal_time: datetime, now: datetime) -> bool:
        return now - signal_time > timedelta(minutes=self.stale_signal_minutes)

    def illiquid(self, spread_pct: float | None, open_interest: int | None) -> str | None:
        if spread_pct is not None and spread_pct > self.max_spread_pct:
            return f'spread {spread_pct}% > {self.max_spread_pct}%'
        if open_interest is not None and open_interest < self.min_open_interest:
            return f'OI {open_interest} < {self.min_open_interest}'
        return None

    def hard_stop_price(self, avg_cost: float) -> float:
        return round(avg_cost * (1 - self.hard_stop_pct / 100), 2)

    def time_stop_reached(self, expiration: str, now: datetime) -> bool:
        """True once it's past TIME_STOP_ET on the contract's expiration day."""
        now_et = now.astimezone(ET)
        if now_et.date().isoformat() < expiration:
            return False
        if now_et.date().isoformat() > expiration:
            return True
        hh, mm = (int(x) for x in self.time_stop_et.split(':'))
        return now_et.time() >= time(hh, mm)
