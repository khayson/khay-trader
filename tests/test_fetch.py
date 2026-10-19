from pathlib import Path

import ccxt
import pandas as pd
import pytest

from trader.data import fetch, store

HOUR = pd.Timedelta(hours=1)
T0 = pd.Timestamp("2024-01-01", tz="UTC")
MS_HOUR = 3_600_000


class FakeExchange:
    """Serves hourly candles for open times in [T0, T0 + hours), minus `missing`."""

    def __init__(self, hours: int, missing: frozenset[int] = frozenset(), fail_first: int = 0):
        base = int(T0.timestamp() * 1000)
        self.candles = [
            [base + h * MS_HOUR, 1.0, 2.0, 0.5, 1.5, float(h)]
            for h in range(hours)
            if h not in missing
        ]
        self.calls: list[int] = []
        self.fail_first = fail_first

    @staticmethod
    def parse_timeframe(timeframe: str) -> int:
        assert timeframe == "1h"
        return 3600

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: int, limit: int) -> list[list[float]]:
        if self.fail_first:
            self.fail_first -= 1
            raise ccxt.NetworkError("boom")
        self.calls.append(since)
        return [c for c in self.candles if c[0] >= since][:limit]


def test_fetch_paginates_and_respects_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "PAGE_LIMIT", 10)
    ex = FakeExchange(hours=50)
    df = fetch.fetch_ohlcv(ex, "BTC/USDT", "1h", T0 + 3 * HOUR, T0 + 35 * HOUR)
    assert df["timestamp"].iloc[0] == T0 + 3 * HOUR
    assert df["timestamp"].iloc[-1] == T0 + 34 * HOUR
    assert len(df) == 32
    assert len(ex.calls) == 4


def test_fetch_stops_when_exchange_has_nothing() -> None:
    ex = FakeExchange(hours=5)
    df = fetch.fetch_ohlcv(ex, "BTC/USDT", "1h", T0 + 10 * HOUR, T0 + 20 * HOUR)
    assert df.empty


def test_fetch_retries_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    ex = FakeExchange(hours=3, fail_first=2)
    assert len(fetch.fetch_ohlcv(ex, "BTC/USDT", "1h", T0, T0 + 3 * HOUR)) == 3
    with pytest.raises(ccxt.NetworkError):
        fetch.fetch_ohlcv(FakeExchange(hours=3, fail_first=5), "BTC/USDT", "1h", T0, T0 + HOUR)


def test_backfill_excludes_forming_candle_and_is_idempotent(tmp_path: Path) -> None:
    ex = FakeExchange(hours=100)
    now = T0 + 20 * HOUR + pd.Timedelta(minutes=30)  # candle opened at T0+20h is still forming
    first = fetch.backfill(ex, "BTC/USDT", "1h", T0, tmp_path, now)
    assert first["timestamp"].iloc[-1] == T0 + 19 * HOUR
    assert len(first) == 20

    calls_before = len(ex.calls)
    again = fetch.backfill(ex, "BTC/USDT", "1h", T0, tmp_path, now)
    pd.testing.assert_frame_equal(again, first)
    assert len(ex.calls) - calls_before <= 1

    later = fetch.backfill(ex, "BTC/USDT", "1h", T0, tmp_path, now + 5 * HOUR)
    assert later["timestamp"].iloc[-1] == T0 + 24 * HOUR
    assert store.integrity_errors(later, HOUR) == []
    assert store.find_gaps(later, HOUR) == []


def test_backfill_extends_history_backwards(tmp_path: Path) -> None:
    ex = FakeExchange(hours=40)
    now = T0 + 40 * HOUR
    fetch.backfill(ex, "BTC/USDT", "1h", T0 + 20 * HOUR, tmp_path, now)
    df = fetch.backfill(ex, "BTC/USDT", "1h", T0, tmp_path, now)
    assert len(df) == 40
    assert store.find_gaps(df, HOUR) == []


def test_backfill_reports_exchange_gaps(tmp_path: Path) -> None:
    ex = FakeExchange(hours=30, missing=frozenset({10, 11}))
    df = fetch.backfill(ex, "BTC/USDT", "1h", T0, tmp_path, T0 + 30 * HOUR)
    assert store.find_gaps(df, HOUR) == [store.Gap(T0 + 10 * HOUR, T0 + 11 * HOUR, 2)]
