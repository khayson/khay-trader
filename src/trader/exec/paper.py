"""Paper broker: realistic market fills from the live order book, simulated resting stops.

Market orders walk the order book for the order size (fill_model "book"), and the fill
keeps the cost model's price alongside so real-vs-modeled slippage can be measured. In
replay/test mode (fill_model "modeled") fills use the cost model exactly, as the backtest
engine does. Stops are simulated as resting stop-market orders on 1m candles: the first
minute whose low reaches the stop fills at min(minute open, stop) less stop slippage.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import pandas as pd

from trader.backtest.costs import Costs
from trader.data.fetch import fetch_ohlcv
from trader.exec.base import Fill

log = logging.getLogger(__name__)


def walk_book(levels: Sequence[Sequence[float]], qty: float) -> tuple[float, bool]:
    """Average price for taking `qty` from best-first [price, amount] levels, and whether
    the visible depth covered it (any remainder is priced at the worst visible level)."""
    if not levels:
        raise ValueError("empty order book")
    remaining, cost, last = qty, 0.0, float(levels[0][0])
    for level in levels:
        price, amount = float(level[0]), float(level[1])
        take = min(remaining, amount)
        cost += take * price
        remaining -= take
        last = price
        if remaining <= 0:
            return cost / qty, True
    return (cost + remaining * last) / qty, False


class PaperBroker:
    def __init__(self, exchange: Any, costs: Costs, fill_model: str = "book") -> None:
        if fill_model not in ("book", "modeled"):
            raise ValueError(f"unknown fill model {fill_model}")
        self.exchange, self.costs, self.fill_model = exchange, costs, fill_model
        self._stops: dict[str, tuple[float, float]] = {}

    def market(
        self, symbol: str, side: str, qty: float, ref_price: float, time: pd.Timestamp, reason: str
    ) -> Fill:
        buy = side == "buy"
        modeled = self.costs.buy_price(ref_price) if buy else self.costs.sell_price(ref_price)
        price = modeled
        if self.fill_model == "book":
            book = self.exchange.fetch_order_book(symbol, limit=100)
            price, covered = walk_book(book["asks"] if buy else book["bids"], qty)
            if not covered:
                log.warning("%s %s %.8g exceeds visible book depth", symbol, side, qty)
        return Fill(
            time,
            symbol,
            side,
            qty,
            price,
            modeled,
            ref_price,
            self.costs.fee_on(qty * price),
            reason,
        )

    def set_stop(self, symbol: str, qty: float, stop_price: float) -> None:
        self._stops[symbol] = (qty, stop_price)

    def cancel_stop(self, symbol: str) -> None:
        self._stops.pop(symbol, None)

    def resting_stops(self) -> dict[str, tuple[float, float]]:
        return dict(self._stops)

    def poll_stop_fills(self, since: pd.Timestamp, until: pd.Timestamp) -> list[Fill]:
        fills = []
        for symbol, (qty, stop) in sorted(self._stops.items()):
            minutes = fetch_ohlcv(self.exchange, symbol, "1m", since, until)
            hit = minutes[minutes["low"] <= stop]
            if hit.empty:
                continue
            first = hit.iloc[0]
            raw = min(float(first["open"]), stop)
            price = self.costs.stop_sell_price(raw)
            fills.append(
                Fill(
                    first["timestamp"],
                    symbol,
                    "sell",
                    qty,
                    price,
                    price,
                    raw,
                    self.costs.fee_on(qty * price),
                    "stop",
                )
            )
        for f in fills:
            del self._stops[f.symbol]
        return fills
