import numpy as np
import pandas as pd
import pytest

from trader.data import store
from trader.data.resample import resample

HOUR = pd.Timedelta(hours=1)
T0 = pd.Timestamp("2024-01-01", tz="UTC")


def hourly(hours: list[int]) -> pd.DataFrame:
    """Candle h: open h, high h+0.5, low h-0.5, close h+0.25, volume 1 (h = hours from T0)."""
    h = np.asarray(hours, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": [T0 + int(x) * HOUR for x in hours],
            "open": h,
            "high": h + 0.5,
            "low": h - 0.5,
            "close": h + 0.25,
            "volume": 1.0,
        }
    )


def test_4h_aggregates_are_exact_and_utc_aligned() -> None:
    out = resample(hourly(list(range(12))), "4h", HOUR)
    assert list(out.columns) == store.COLUMNS
    assert list(out["timestamp"]) == [T0, T0 + 4 * HOUR, T0 + 8 * HOUR]
    first = out.iloc[0]
    assert (first["open"], first["high"], first["low"], first["close"], first["volume"]) == (
        0.0,
        3.5,
        -0.5,
        3.25,
        4.0,
    )
    assert out.iloc[2]["open"] == 8.0 and out.iloc[2]["close"] == 11.25
    assert store.integrity_errors(out, pd.Timedelta(hours=4)) == []


def test_incomplete_bars_are_dropped() -> None:
    # hour 5 missing -> bar [4,8) incomplete; trailing bar [8,12) has only hours 8-9 (still forming)
    out = resample(hourly([0, 1, 2, 3, 4, 6, 7, 8, 9]), "4h", HOUR)
    assert list(out["timestamp"]) == [T0]


def test_leading_partial_bar_is_dropped() -> None:
    out = resample(hourly(list(range(2, 12))), "4h", HOUR)
    assert list(out["timestamp"]) == [T0 + 4 * HOUR, T0 + 8 * HOUR]


def test_documented_outage_hours_do_not_drop_a_bar() -> None:
    outage = store.Gap(start=T0 + 5 * HOUR, end=T0 + 6 * HOUR, missing=2)
    out = resample(hourly([0, 1, 2, 3, 4, 7, 8, 9, 10, 11]), "4h", HOUR, [outage])
    assert list(out["timestamp"]) == [T0, T0 + 4 * HOUR, T0 + 8 * HOUR]
    bar = out.iloc[1]
    assert (bar["open"], bar["close"], bar["volume"]) == (4.0, 7.25, 2.0)


def test_bar_entirely_inside_outage_is_absent() -> None:
    outage = store.Gap(start=T0 + 4 * HOUR, end=T0 + 7 * HOUR, missing=4)
    out = resample(hourly([0, 1, 2, 3, 8, 9, 10, 11]), "4h", HOUR, [outage])
    assert list(out["timestamp"]) == [T0, T0 + 8 * HOUR]


def test_daily_is_midnight_aligned() -> None:
    df = hourly(list(range(-3, 48)))  # starts 21:00 the day before
    out = resample(df, "1D", HOUR)
    assert list(out["timestamp"]) == [T0, T0 + 24 * HOUR]
    assert out.iloc[0]["high"] == 23.5 and out.iloc[0]["volume"] == 24.0


def test_rejects_non_multiple_rule() -> None:
    with pytest.raises(ValueError):
        resample(hourly([0, 1]), "90min", HOUR)
