"""Rule-based baseline (SPEC section 5): long-only trend following with an ATR trailing stop.

Stops are evaluated on the close, consistent with signals on close and fills at next open.
Size is priced at the signal close; the next open will differ slightly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trader.state.snapshot import Snapshot
from trader.strategy.base import Action, Intent, Position


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


class Baseline:
    def __init__(self, params: BaselineParams) -> None:
        self.p = params

    def decide(self, snap: Snapshot, position: Position | None, equity: float) -> Intent:
        trail = snap.close - self.p.stop_atr_mult * snap.atr
        if position is not None:
            if snap.close < snap.ema_fast or snap.close <= position.stop_price:
                return Intent(Action.EXIT, size=position.qty)
            return Intent(Action.HOLD, stop_price=max(position.stop_price, trail))

        trending = snap.close > snap.ema_fast > snap.ema_slow
        sane_vol = self.p.atr_pct_min <= snap.atr_pct <= self.p.atr_pct_max
        if not (trending and sane_vol) or trail <= 0:
            return Intent(Action.HOLD)
        size = self.size(equity, snap.close, trail)
        if size <= 0:
            return Intent(Action.HOLD)
        return Intent(Action.ENTER, size=size, stop_price=trail)

    def size(self, equity: float, price: float, stop: float) -> float:
        """Fixed-fractional risk over stop distance, capped by max position notional."""
        by_risk = equity * self.p.risk_per_trade_pct / 100 / (price - stop)
        cap = equity * self.p.max_position_pct / 100 / price
        return max(0.0, min(by_risk, cap))
