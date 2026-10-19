"""Strategy interface. A strategy turns a snapshot into an intent; it never places orders.

An intent computed on the candle closing at t is executed at the open of the next candle.
"""

from __future__ import annotations

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
