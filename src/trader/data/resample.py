"""Deterministic resampling of candles to coarser, UTC-aligned bars (closed bars only)."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from trader.data.store import COLUMNS, Gap


def resample(
    df: pd.DataFrame, rule: str, base_step: pd.Timedelta, outages: Sequence[Gap] = ()
) -> pd.DataFrame:
    """Aggregate `df` (canonical candles of `base_step`) into `rule` bars keyed by open time.

    A bar is kept only if every constituent candle is present, except candles inside a
    documented outage (no trading happened then). This also drops the still-forming last
    bar and any partial first bar. Bars with no candles at all are absent.
    """
    step = pd.Timedelta(rule)
    if step <= base_step or step % base_step != pd.Timedelta(0):
        raise ValueError(f"{rule} is not a multiple of {base_step}")
    per_bar = int(step / base_step)

    grouped = df.set_index("timestamp").resample(step, origin="epoch", label="left", closed="left")
    out = grouped.agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    count = grouped["close"].count()

    outage_times = [t for g in outages for t in pd.date_range(g.start, g.end, freq=base_step)]
    outage_count = (
        pd.Series(1, index=pd.DatetimeIndex(outage_times, tz="UTC"))
        .resample(step, origin="epoch")
        .sum()
        .reindex(out.index, fill_value=0)
        if outage_times
        else 0
    )
    keep = (count > 0) & (count + outage_count == per_bar)
    result = out[keep].reset_index()
    return result[COLUMNS].astype({c: "float64" for c in COLUMNS[1:]})
