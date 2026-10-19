"""Fee and slippage model. Fees are a fraction of notional; slippage always moves price
against us."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Costs:
    fee: float  # fraction of notional, per side
    slippage: float  # fraction of price, per side

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> Costs:
        return cls(fee=cfg["fees_per_side"], slippage=cfg["slippage_per_side"])

    def buy_price(self, price: float) -> float:
        return price * (1 + self.slippage)

    def sell_price(self, price: float) -> float:
        return price * (1 - self.slippage)

    def fee_on(self, notional: float) -> float:
        return notional * self.fee


ZERO = Costs(fee=0.0, slippage=0.0)
