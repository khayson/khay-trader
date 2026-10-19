"""Deterministic feature snapshot.

Timing rule: candles are keyed by open time; a candle closes at open + step. A snapshot
at time t uses only candles whose close time is <= t. `features()` builds the whole
table with causal operations only, so row i depends on candles 0..i and nothing later.
Windows are counted in bars, so input must be gap-free (see config/known_gaps.yaml).

Units: atr_pct is a percent of close; returns and drawdown are fractions; realized_vol
is the annualized std of 1-bar log returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

FEATURES = (
    "close",
    "ema_fast",
    "ema_slow",
    "atr",
    "atr_pct",
    "ret_24h",
    "ret_7d",
    "realized_vol",
    "drawdown",
)


@dataclass(frozen=True)
class FeatureParams:
    ema_fast: int
    ema_slow: int
    atr_period: int
    vol_window_hours: int
    drawdown_window_hours: int

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> FeatureParams:
        return cls(**cfg["features"])

    def warmup_bars(self, step: pd.Timedelta) -> int:
        """Candles needed before every feature is defined and the EMAs have converged.

        5 * ema_slow bars leaves ~0.005% weight on the first-candle seed, so features
        agree regardless of how much history was loaded.
        """
        return max(
            5 * self.ema_slow,
            self.atr_period,
            _bars(pd.Timedelta(days=7), step) + 1,
            _bars(pd.Timedelta(hours=self.vol_window_hours), step) + 1,
            _bars(pd.Timedelta(hours=self.drawdown_window_hours), step),
        )


@dataclass(frozen=True)
class Snapshot:
    time: pd.Timestamp  # close time of the last candle used
    close: float
    ema_fast: float
    ema_slow: float
    atr: float
    atr_pct: float
    ret_24h: float
    ret_7d: float
    realized_vol: float
    drawdown: float


def _bars(span: pd.Timedelta, step: pd.Timedelta) -> int:
    return int(span / step)


def ema(series: pd.Series, span: int) -> pd.Series:
    """Recursive EMA, alpha = 2 / (span + 1), seeded with the first value."""
    return series.ewm(span=span, adjust=False).mean()


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """ATR with Wilder smoothing, seeded with the SMA of the first `period` true ranges."""
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    values = tr.to_numpy(dtype="float64")
    out = np.full(len(values), np.nan)
    if len(values) >= period:
        out[period - 1] = values[:period].mean()
        for i in range(period, len(values)):
            out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return pd.Series(out, index=close.index)


def features(candles: pd.DataFrame, step: pd.Timedelta, p: FeatureParams) -> pd.DataFrame:
    """Feature table indexed by candle close time, with a `ready` flag per row."""
    c = candles.reset_index(drop=True)
    close = c["close"]
    atr = wilder_atr(c["high"], c["low"], close, p.atr_period)
    vol_bars = _bars(pd.Timedelta(hours=p.vol_window_hours), step)
    df = pd.DataFrame(
        {
            "close": close,
            "ema_fast": ema(close, p.ema_fast),
            "ema_slow": ema(close, p.ema_slow),
            "atr": atr,
            "atr_pct": 100 * atr / close,
            "ret_24h": close / close.shift(_bars(pd.Timedelta(hours=24), step)) - 1,
            "ret_7d": close / close.shift(_bars(pd.Timedelta(days=7), step)) - 1,
            "realized_vol": np.log(close).diff().rolling(vol_bars).std()
            * np.sqrt(_bars(pd.Timedelta(days=365), step)),
            "drawdown": close
            / close.rolling(_bars(pd.Timedelta(hours=p.drawdown_window_hours), step)).max()
            - 1,
        }
    )
    df.index = pd.DatetimeIndex(c["timestamp"] + step, name="close_time")
    df["ready"] = (np.arange(len(df)) >= p.warmup_bars(step) - 1) & df.notna().all(axis=1)
    return df


def snapshot_at(feats: pd.DataFrame, t: pd.Timestamp) -> Snapshot | None:
    """Snapshot from a precomputed feature table; None if not ready at t."""
    closed = feats.loc[:t]
    if closed.empty or not closed["ready"].iloc[-1]:
        return None
    row = closed.iloc[-1]
    return Snapshot(time=closed.index[-1], **{f: float(row[f]) for f in FEATURES})


def snapshot(
    candles: pd.DataFrame, t: pd.Timestamp, step: pd.Timedelta, p: FeatureParams
) -> Snapshot | None:
    """Snapshot at t from raw candles, using only candles closed at or before t."""
    closed = candles[candles["timestamp"] + step <= t]
    return snapshot_at(features(closed, step, p), t)
