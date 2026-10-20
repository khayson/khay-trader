"""Deterministic in-memory exchange for paper-loop tests (ccxt-shaped, public data only)."""

from __future__ import annotations

from typing import Any

import ccxt
import numpy as np
import pandas as pd

from trader.data.resample import resample

MINUTE = pd.Timedelta(minutes=1)


def make_minutes(start: pd.Timestamp, hours: int, seed: int) -> pd.DataFrame:
    """Trending random walk; each minute opens at the previous close (no intra-bar gaps)."""
    rng = np.random.default_rng(seed)
    n = hours * 60
    t = np.arange(n)
    drift = 0.00006 * np.sin(2 * np.pi * t / (60 * 24 * 5) + seed)
    shocks = np.where(rng.random(n) < 1 / 2500, -0.10, 0.0)  # sudden drops hit stops
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.0007, n) + shocks))
    open_ = np.concatenate([[100.0], close[:-1]])
    wick = np.abs(rng.normal(0, 0.0003, n))
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(start, periods=n, freq="1min"),
            "open": open_,
            "high": np.maximum(open_, close) * (1 + wick),
            "low": np.minimum(open_, close) * (1 - wick),
            "close": close,
            "volume": rng.uniform(1, 5, n),
        }
    )


class FakeExchange:
    STEPS = {"1m": 60, "1h": 3600, "4h": 14400}

    def __init__(self, minutes: dict[str, pd.DataFrame], now: pd.Timestamp) -> None:
        self.now = now
        self.fail_next = 0  # raise NetworkError on the next N calls
        self.feed_cutoff: pd.Timestamp | None = None  # hide 1h/4h candles opening at/after
        self.native_overrides: dict[tuple[str, pd.Timestamp], float] = {}  # 4h close tweaks
        self.frames = {
            s: {"1m": m, "1h": resample(m, "1h", MINUTE), "4h": resample(m, "4h", MINUTE)}
            for s, m in minutes.items()
        }

    @staticmethod
    def parse_timeframe(timeframe: str) -> int:
        return FakeExchange.STEPS[timeframe]

    def _maybe_fail(self) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ccxt.NetworkError("fake exchange unavailable")

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[float]]:
        self._maybe_fail()
        df = self.frames[symbol][timeframe]
        visible = df[df["timestamp"] <= self.now]  # includes the forming candle (open only used)
        if self.feed_cutoff is not None and timeframe != "1m":
            visible = visible[visible["timestamp"] < self.feed_cutoff]
        if since is not None:
            visible = visible[visible["timestamp"] >= pd.Timestamp(since, unit="ms", tz="UTC")]
        visible = visible.head(limit or 1000)
        rows: list[list[float]] = []
        for ts, o, h, lo, c, v in zip(*(visible[col] for col in visible.columns), strict=True):
            t = pd.Timestamp(ts)
            if timeframe == "4h":
                c = self.native_overrides.get((symbol, t), c)
            rows.append(
                [int(t.timestamp() * 1000), float(o), float(h), float(lo), float(c), float(v)]
            )
        return rows

    def fetch_order_book(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        self._maybe_fail()
        m = self.frames[symbol]["1m"]
        price = float(m.loc[m["timestamp"] <= self.now, "open"].iloc[-1])
        asks = [[price * (1 + 0.0002 + 0.0001 * i), 50.0] for i in range(limit)]
        bids = [[price * (1 - 0.0002 - 0.0001 * i), 50.0] for i in range(limit)]
        return {"asks": asks, "bids": bids}
