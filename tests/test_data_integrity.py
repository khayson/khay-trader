"""Integrity of the real backfilled candles in data/. Skipped until a backfill has run.

Gaps are allowed here only because exchange outages are real; they must appear in
reports/data_gaps.md, which the backfill CLI regenerates.
"""

from pathlib import Path

import pandas as pd
import pytest

from trader.data import store

FILES = sorted((Path(__file__).parent.parent / "data" / "candles").glob("*_1h.parquet"))


@pytest.mark.skipif(not FILES, reason="no backfilled candles; run trader-backfill")
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_stored_candles_are_clean(path: Path) -> None:
    df = store.load(path)
    assert not df.empty
    assert store.integrity_errors(df, pd.Timedelta(hours=1)) == []
    assert df["timestamp"].iloc[-1] < pd.Timestamp.now(tz="UTC").floor("1h")  # closed only
