"""Paper broker: realistic market fills from the live order book, simulated resting stops.

Market orders walk the order book for the order size (fill_model "book") and record the
book mid and spread at that moment next to the cost model's price, so execution impact
(fill vs mid) can be told apart from drift (mid vs the bar-open reference). In replay/test
mode (fill_model "modeled") fills use the cost model exactly, as the backtest engine does.

Stops are simulated as resting stop-market orders on 1m candles: the first minute whose
low reaches the stop fills at min(minute open, stop) less stop slippage.

Orders respect the exchange's amount precision, lot size and minimum notional (ccxt
markets), so behaviour at the real account size is exercised.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import ccxt
import pandas as pd

from trader.backtest.costs import Costs
from trader.data.fetch import fetch_ohlcv, retry_call
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
    def __init__(
        self,
        exchange: Any,
        costs: Costs,
        fill_model: str = "book",
        retry_delays: Sequence[float] = (5.0, 25.0),
    ) -> None:
        if fill_model not in ("book", "modeled"):
            raise ValueError(f"unknown fill model {fill_model}")
        self.exchange, self.costs, self.fill_model = exchange, costs, fill_model
        self.retry_delays = tuple(retry_delays)
        self._stops: dict[str, tuple[float, float]] = {}
        self._markets_loaded = False

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return retry_call(fn, *args, delays=self.retry_delays, **kwargs)

    def _book(self, symbol: str) -> dict[str, Any]:
        book: dict[str, Any] = self._call(self.exchange.fetch_order_book, symbol, limit=100)
        if not book["bids"] or not book["asks"]:
            raise ccxt.ExchangeError(f"{symbol}: empty order book")
        return book

    def mid(self, symbol: str) -> float:
        book = self._book(symbol)
        return (float(book["bids"][0][0]) + float(book["asks"][0][0])) / 2

    def conform(self, symbol: str, qty: float, price: float) -> tuple[float, str | None]:
        if not self._markets_loaded:
            self._call(self.exchange.load_markets)
            self._markets_loaded = True
        market = self.exchange.market(symbol)
        try:
            q = float(self.exchange.amount_to_precision(symbol, qty))
        except ccxt.InvalidOrder:
            q = 0.0
        limits = market.get("limits", {})
        min_amount = float((limits.get("amount") or {}).get("min") or 0.0)
        min_cost = float((limits.get("cost") or {}).get("min") or 0.0)
        if q <= 0 or q < min_amount:
            return 0.0, f"exchange minimum: amount {qty:.10g} below {min_amount:.10g}"
        if q * price < min_cost:
            return 0.0, f"exchange minimum: notional {q * price:.2f} below {min_cost:.2f}"
        return q, None

    def market(
        self, symbol: str, side: str, qty: float, ref_price: float, time: pd.Timestamp, reason: str
    ) -> Fill:
        buy = side == "buy"
        modeled = self.costs.buy_price(ref_price) if buy else self.costs.sell_price(ref_price)
        price, mid, spread = modeled, None, None
        if self.fill_model == "book":
            book = self._book(symbol)
            bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
            mid, spread = (bid + ask) / 2, ask - bid
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
            mid,
            spread,
        )

    def set_stop(self, symbol: str, qty: float, stop_price: float) -> None:
        self._stops[symbol] = (qty, stop_price)

    def cancel_stop(self, symbol: str) -> None:
        self._stops.pop(symbol, None)

    def resting_stops(self) -> dict[str, tuple[float, float]]:
        return dict(self._stops)

    def poll_stop_fills(self, since: pd.Timestamp, until: pd.Timestamp) -> list[Fill]:
        fills: list[Fill] = []
        if until <= since:
            return fills
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
                    pd.Timestamp(first["timestamp"]),
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
