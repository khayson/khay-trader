"""Pre-trade risk limits (SPEC section 6). Pure functions: time is injected, no I/O.

Policy decisions:
- Every new entry passes `pre_trade_check`. Exits and stop fills are never blocked (they
  only reduce risk) but still count toward the order rate.
- Caps are checked at fill time on notional at the fill price. A position that later grows
  past `max_position_pct` is NOT trimmed (matches the backtest); adding to it is rejected
  by the position cap and `overcap_symbols` logs a warning so the operator sees it.
- Boundaries: an order exactly at a cap is allowed; a daily loss exactly at the limit, or an
  order that would be the N+1th in the trailing hour, is rejected.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

_REL_TOL = 1e-12  # float slack so an order sized exactly to a cap is not rejected


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float
    max_total_exposure_pct: float
    max_daily_loss_pct: float
    max_orders_per_hour: int
    stale_data_seconds: float

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> RiskLimits:
        r = cfg["risk"]
        return cls(
            max_position_pct=r["max_position_pct"],
            max_total_exposure_pct=r["max_total_exposure_pct"],
            max_daily_loss_pct=r["max_daily_loss_pct"],
            max_orders_per_hour=r["max_orders_per_hour"],
            stale_data_seconds=r["stale_data_seconds"],
        )


@dataclass(frozen=True)
class Account:
    equity: float  # marked at current prices
    position_values: Mapping[str, float]  # symbol -> marked notional held
    day_start_equity: float  # equity at the start of the current UTC day


@dataclass(frozen=True)
class EntryOrder:
    symbol: str
    notional: float  # quantity x expected fill price


@dataclass(frozen=True)
class Approved:
    pass


@dataclass(frozen=True)
class Rejected:
    reason: str


def _utc(ts: pd.Timestamp, name: str) -> None:
    if ts.tzinfo is None or str(ts.tz) != "UTC":
        raise ValueError(f"{name} must be a UTC timestamp, got {ts!r}")


def _within(value: float, cap: float) -> bool:
    return value <= cap + abs(cap) * _REL_TOL


def entry_headroom(account: Account, symbol: str, limits: RiskLimits) -> float:
    """Largest new notional in `symbol` that both caps allow (0 if none)."""
    held = account.position_values.get(symbol, 0.0)
    total = sum(account.position_values.values())
    by_position = account.equity * limits.max_position_pct / 100 - held
    by_total = account.equity * limits.max_total_exposure_pct / 100 - total
    return max(0.0, min(by_position, by_total))


def overcap_symbols(account: Account, limits: RiskLimits, log_warnings: bool = True) -> list[str]:
    """Positions that grew past the position cap after fill; optionally logs a warning for
    each (callers that run every bar throttle the warning themselves)."""
    cap = account.equity * limits.max_position_pct / 100
    over = sorted(s for s, v in account.position_values.items() if not _within(v, cap))
    for s in over if log_warnings else []:
        log.warning(
            "%s is %.2f%% of equity, over the %s%% cap; no auto-trim, new entries blocked",
            s,
            100 * account.position_values[s] / account.equity,
            limits.max_position_pct,
        )
    return over


def data_freshness(
    last_bar_close: pd.Timestamp, now: pd.Timestamp, step: pd.Timedelta, limits: RiskLimits
) -> Rejected | None:
    """The newest closed bar must be the one that closed most recently, and we must be
    acting on it within `stale_data_seconds` of its close."""
    _utc(last_bar_close, "last_bar_close")
    _utc(now, "now")
    expected = now.floor(step)
    if last_bar_close != expected:
        return Rejected(f"stale data: last closed bar {last_bar_close}, expected {expected}")
    age = (now - last_bar_close).total_seconds()
    if age > limits.stale_data_seconds:
        return Rejected(f"stale data: bar closed {age:.0f}s ago (> {limits.stale_data_seconds}s)")
    return None


def daily_loss(account: Account, limits: RiskLimits) -> Rejected | None:
    loss = account.day_start_equity - account.equity
    if loss >= account.day_start_equity * limits.max_daily_loss_pct / 100:
        return Rejected(
            f"daily loss {100 * loss / account.day_start_equity:.2f}% >= "
            f"{limits.max_daily_loss_pct}%"
        )
    return None


def order_rate(
    recent_orders: Sequence[pd.Timestamp], now: pd.Timestamp, limits: RiskLimits
) -> Rejected | None:
    """Orders in the trailing hour (now - 1h, now]; a new one must not exceed the limit."""
    window_start = now - pd.Timedelta(hours=1)
    count = sum(1 for t in recent_orders if window_start < t <= now)
    if count >= limits.max_orders_per_hour:
        return Rejected(
            f"order rate: {count} orders in the last hour (max {limits.max_orders_per_hour})"
        )
    return None


def exposure(order: EntryOrder, account: Account, limits: RiskLimits) -> Rejected | None:
    held = account.position_values.get(order.symbol, 0.0)
    position_cap = account.equity * limits.max_position_pct / 100
    if not _within(held + order.notional, position_cap):
        return Rejected(
            f"position cap: {order.symbol} would be {held + order.notional:.2f} "
            f"> {position_cap:.2f}"
        )
    total = sum(account.position_values.values()) + order.notional
    total_cap = account.equity * limits.max_total_exposure_pct / 100
    if not _within(total, total_cap):
        return Rejected(f"total exposure would be {total:.2f} > {total_cap:.2f}")
    return None


def pre_trade_check(
    order: EntryOrder,
    account: Account,
    limits: RiskLimits,
    recent_orders: Sequence[pd.Timestamp],
    last_bar_close: pd.Timestamp,
    now: pd.Timestamp,
    step: pd.Timedelta,
) -> Approved | Rejected:
    """All entry rules; the first failure is returned."""
    for result in (
        data_freshness(last_bar_close, now, step, limits),
        daily_loss(account, limits),
        order_rate(recent_orders, now, limits),
        exposure(order, account, limits),
    ):
        if result is not None:
            return result
    return Approved()
