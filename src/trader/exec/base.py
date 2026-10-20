"""Broker interface shared by paper and (later) live execution.

Protective stops are resting stop-market orders held by the broker (SPEC 6.6): the loop
sets or replaces them at bar closes and cancels one only after its position is closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd


@dataclass(frozen=True)
class Fill:
    time: pd.Timestamp
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    price: float  # actual (paper: order-book walk or simulated stop) fill price
    modeled_price: float  # what the backtest cost model would have charged
    raw_price: float  # reference price before slippage (bar open or stop level)
    fee: float
    reason: str  # entry | signal | stop | killswitch
    mid: float | None = None  # order-book mid when filled (None for simulated/modeled fills)
    spread: float | None = None  # best ask - best bid when filled


class Broker(Protocol):
    def market(
        self, symbol: str, side: str, qty: float, ref_price: float, time: pd.Timestamp, reason: str
    ) -> Fill: ...

    def set_stop(self, symbol: str, qty: float, stop_price: float) -> None: ...

    def cancel_stop(self, symbol: str) -> None: ...

    def resting_stops(self) -> dict[str, tuple[float, float]]: ...

    def conform(self, symbol: str, qty: float, price: float) -> tuple[float, str | None]:
        """Apply the exchange's amount precision and minimums: (qty, None) or (0, reason)."""
        ...

    def mid(self, symbol: str) -> float: ...

    def poll_stop_fills(self, since: pd.Timestamp, until: pd.Timestamp) -> list[Fill]:
        """Stops triggered in [since, until); each triggered stop is removed."""
        ...
