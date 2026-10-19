"""Candle storage (Parquet) and timestamp integrity checks.

Canonical candle frame: columns COLUMNS, `timestamp` is the candle open time as a
tz-aware UTC datetime, prices/volume are float64, rows sorted and unique by timestamp.
SQLite helpers for orders/trades arrive with the paper loop (Phase 5).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class Gap:
    start: pd.Timestamp  # first missing candle open time
    end: pd.Timestamp  # last missing candle open time
    missing: int


def candle_path(data_dir: Path, symbol: str, timeframe: str) -> Path:
    return data_dir / "candles" / f"{symbol.replace('/', '-')}_{timeframe}.parquet"


def to_frame(rows: list[list[float]]) -> pd.DataFrame:
    """ccxt OHLCV rows ([ms, open, high, low, close, volume]) -> canonical frame."""
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
    return df.astype({c: "float64" for c in COLUMNS[1:]})


def load(path: Path) -> pd.DataFrame:
    if not path.exists():
        return to_frame([])
    return pd.read_parquet(path)


def merge(*frames: pd.DataFrame) -> pd.DataFrame:
    """Union of frames; on duplicate timestamps the later frame wins."""
    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("timestamp", keep="last")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def save(df: pd.DataFrame, path: Path, step: pd.Timedelta) -> None:
    """Validate, then write atomically so a crash never leaves a truncated file."""
    errors = integrity_errors(df, step)
    if errors:
        raise ValueError(f"refusing to save {path.name}: {'; '.join(errors)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def integrity_errors(df: pd.DataFrame, step: pd.Timedelta) -> list[str]:
    """Structural problems (not gaps): timezone, duplicates, ordering, alignment, NaNs."""
    ts = df["timestamp"]
    if str(getattr(ts.dt, "tz", None)) != "UTC":
        return [f"timestamps are not UTC (tz={getattr(ts.dt, 'tz', None)})"]
    errors = []
    if ts.duplicated().any():
        errors.append(f"{int(ts.duplicated().sum())} duplicate timestamps")
    if not ts.is_monotonic_increasing:
        errors.append("timestamps not sorted ascending")
    if (ts.dt.floor(step) != ts).any():
        errors.append(f"timestamps not aligned to {step}")
    if df[COLUMNS[1:]].isna().any().any():
        errors.append("NaN values in OHLCV")
    return errors


def find_gaps(df: pd.DataFrame, step: pd.Timedelta) -> list[Gap]:
    ts = df["timestamp"]
    deltas = ts.diff()
    gaps = []
    for i in deltas.index[deltas > step]:
        prev = ts[i - 1]
        gaps.append(Gap(start=prev + step, end=ts[i] - step, missing=int(deltas[i] / step) - 1))
    return gaps
