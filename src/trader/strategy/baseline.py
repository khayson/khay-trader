"""Rule-based baseline (SPEC section 5): long-only trend following with an ATR trailing stop.

Stops are evaluated on the close, consistent with signals on close and fills at next open.
Size is priced at the signal close; the next open will differ slightly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trader.state.snapshot import Snapshot
from trader.strategy.base import StopSizing, TrailingStopStrategy


@dataclass(frozen=True)
class BaselineParams:
    atr_pct_min: float
    atr_pct_max: float
    stop_atr_mult: float
    risk_per_trade_pct: float
    max_position_pct: float

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> BaselineParams:
        risk = cfg["risk"]
        return cls(
            **cfg["strategy"],
            risk_per_trade_pct=risk["risk_per_trade_pct"],
            max_position_pct=risk["max_position_pct"],
        )


class Baseline(TrailingStopStrategy):
    def __init__(self, params: BaselineParams) -> None:
        self.p = params
        self.sizing = StopSizing(
            params.stop_atr_mult, params.risk_per_trade_pct, params.max_position_pct
        )

    def entry_signal(self, snap: Snapshot) -> bool:
        trending = snap.close > snap.ema_fast > snap.ema_slow
        return trending and self.p.atr_pct_min <= snap.atr_pct <= self.p.atr_pct_max

    def exit_signal(self, snap: Snapshot) -> bool:
        return snap.close < snap.ema_fast
