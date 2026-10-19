from pathlib import Path

import pandas as pd
import pytest

from trader.data import store

HOUR = pd.Timedelta(hours=1)
T0 = pd.Timestamp("2024-01-01", tz="UTC")


def rows(hours: list[int]) -> list[list[float]]:
    base = int(T0.timestamp() * 1000)
    return [[base + h * 3_600_000, 1.0, 2.0, 0.5, 1.5, 10.0] for h in hours]


def test_to_frame_is_utc_and_typed() -> None:
    df = store.to_frame(rows([0, 1]))
    assert list(df.columns) == store.COLUMNS
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert df["timestamp"].iloc[0] == T0
    assert all(df[c].dtype == "float64" for c in store.COLUMNS[1:])


def test_merge_dedupes_and_sorts_keeping_latest() -> None:
    old = store.to_frame(rows([2, 0, 1]))
    new = store.to_frame([[r[0], 9, 9, 9, 9, 9] for r in rows([1, 3])])
    merged = store.merge(old, new)
    assert list(merged["timestamp"]) == [T0 + h * HOUR for h in range(4)]
    assert merged.loc[1, "close"] == 9


def test_find_gaps() -> None:
    df = store.to_frame(rows([0, 1, 4, 5, 7]))
    gaps = store.find_gaps(df, HOUR)
    assert gaps == [
        store.Gap(start=T0 + 2 * HOUR, end=T0 + 3 * HOUR, missing=2),
        store.Gap(start=T0 + 6 * HOUR, end=T0 + 6 * HOUR, missing=1),
    ]
    assert store.find_gaps(store.to_frame(rows([0, 1, 2])), HOUR) == []


def test_integrity_clean() -> None:
    assert store.integrity_errors(store.to_frame(rows([0, 1, 3])), HOUR) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda df: pd.concat([df, df.iloc[[0]]]).sort_values("timestamp"), "duplicate"),
        (lambda df: df.iloc[::-1], "not sorted"),
        (lambda df: df.assign(timestamp=df["timestamp"].dt.tz_localize(None)), "UTC"),
        (lambda df: df.assign(timestamp=df["timestamp"] + pd.Timedelta(minutes=1)), "aligned"),
        (lambda df: df.assign(close=float("nan")), "NaN"),
    ],
)
def test_integrity_errors_detected(mutate, expected) -> None:  # type: ignore[no-untyped-def]
    df = mutate(store.to_frame(rows([0, 1, 2])))
    errors = store.integrity_errors(df, HOUR)
    assert any(expected in e for e in errors), errors


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = store.candle_path(tmp_path, "BTC/USDT", "1h")
    assert path.name == "BTC-USDT_1h.parquet"
    assert store.load(path).empty
    df = store.to_frame(rows([0, 1]))
    store.save(df, path, HOUR)
    pd.testing.assert_frame_equal(store.load(path), df)


def test_save_refuses_corrupt_frame(tmp_path: Path) -> None:
    path = tmp_path / "x.parquet"
    df = store.to_frame(rows([0, 0]))
    with pytest.raises(ValueError, match="duplicate"):
        store.save(df, path, HOUR)
    assert not path.exists()
