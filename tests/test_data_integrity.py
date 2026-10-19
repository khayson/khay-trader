"""Integrity of the real backfilled candles in data/. Skipped until a backfill has run.

Any gap fails unless it is listed in config/known_gaps.yaml.
"""

from pathlib import Path

import pandas as pd
import pytest

from trader.data import store

ROOT = Path(__file__).parent.parent
FILES = sorted((ROOT / "data" / "candles").glob("*_1h.parquet"))


@pytest.mark.skipif(not FILES, reason="no backfilled candles; run trader-backfill")
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_stored_candles_are_clean(path: Path) -> None:
    step = pd.Timedelta(hours=1)
    df = store.load(path)
    assert not df.empty
    assert store.integrity_errors(df, step) == []
    assert df["timestamp"].iloc[-1] < pd.Timestamp.now(tz="UTC").floor(step)  # closed only

    symbol = path.stem.removesuffix("_1h").replace("-", "/")
    found = {(g.start, g.end) for g in store.find_gaps(df, step)}
    allowed = {
        (g.start, g.end)
        for g in store.load_known_gaps(ROOT / "config" / "known_gaps.yaml", symbol, "1h")
    }
    assert found <= allowed, f"unlisted gaps: {sorted(found - allowed)}"
