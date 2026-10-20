"""Strategy interface. A strategy turns a snapshot into an intent; it never places orders.

An intent computed on the candle closing at t is executed at the open of the next candle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from trader.state.snapshot import Snapshot


class Action(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"


@dataclass(frozen=True)
class Position:
    qty: float  # base-asset units held
    entry_price: float
    stop_price: float


@dataclass(frozen=True)
class Intent:
    action: Action
    size: float = 0.0  # base-asset units to buy (ENTER) or sell (EXIT)
    stop_price: float | None = None  # stop to hold after this bar (ENTER/HOLD with a position)


class Strategy(Protocol):
    def decide(self, snap: Snapshot, position: Position | None, equity: float) -> Intent: ...


@dataclass(frozen=True)
class StopSizing:
    stop_atr_mult: float  # initial and trailing stop distance, in ATRs
    risk_per_trade_pct: float
    max_position_pct: float


def position_size(equity: float, price: float, stop: float, sizing: StopSizing) -> float:
    """Fixed-fractional risk over stop distance, capped by max position notional."""
    by_risk = equity * sizing.risk_per_trade_pct / 100 / (price - stop)
    cap = equity * sizing.max_position_pct / 100 / price
    return max(0.0, min(by_risk, cap))


class TrailingStopStrategy(ABC):
    """Long-only template: subclasses define entry/exit signals; this adds an ATR stop that
    ratchets up at closes (exit when close <= stop) and fixed-fractional sizing."""

    sizing: StopSizing

    @abstractmethod
    def entry_signal(self, snap: Snapshot) -> bool: ...

    @abstractmethod
    def exit_signal(self, snap: Snapshot) -> bool: ...

    def decide(self, snap: Snapshot, position: Position | None, equity: float) -> Intent:
        trail = snap.close - self.sizing.stop_atr_mult * snap.atr
        if position is not None:
            if self.exit_signal(snap) or snap.close <= position.stop_price:
                return Intent(Action.EXIT, size=position.qty)
            return Intent(Action.HOLD, stop_price=max(position.stop_price, trail))
        if not self.entry_signal(snap) or trail <= 0:
            return Intent(Action.HOLD)
        size = position_size(equity, snap.close, trail, self.sizing)
        if size <= 0:
            return Intent(Action.HOLD)
        return Intent(Action.ENTER, size=size, stop_price=trail)
