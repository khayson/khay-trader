import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from trader.state import snapshot as snap
from trader.state.snapshot import FeatureParams

HOUR = pd.Timedelta(hours=1)
T0 = pd.Timestamp("2024-01-01", tz="UTC")
CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config" / "default.yaml").read_text())
PARAMS = FeatureParams.from_config(CONFIG)


def random_candles(n: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    spread = np.abs(rng.normal(0, 0.005, n)) * close
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(T0, periods=n, freq="1h"),
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(1, 10, n),
        }
    )


FULL = random_candles(800)
# Close times to probe: before warmup, at the first ready bar, mid-series, mid-candle, near the end.
PROBES = [
    T0 + 50 * HOUR,
    T0 + 200 * HOUR,
    T0 + 201 * HOUR,
    T0 + 450 * HOUR,
    T0 + 450 * HOUR + pd.Timedelta(minutes=30),
    T0 + 799 * HOUR,
]


# --- look-ahead -----------------------------------------------------------------------------


@pytest.mark.parametrize("t", PROBES)
def test_snapshot_from_full_data_equals_snapshot_from_truncated_data(t: pd.Timestamp) -> None:
    truncated = FULL[FULL["timestamp"] + HOUR <= t]
    from_full = snap.snapshot_at(snap.features(FULL, HOUR, PARAMS), t)
    from_truncated = snap.snapshot_at(snap.features(truncated, HOUR, PARAMS), t)
    assert from_full == from_truncated


@pytest.mark.parametrize("t", PROBES)
def test_mutating_the_future_does_not_change_the_snapshot(t: pd.Timestamp) -> None:
    rng = np.random.default_rng(99)
    future = FULL["timestamp"] + HOUR > t
    mutated = FULL.copy()
    n = int(future.sum())
    noise = rng.uniform(1, 10_000, n)
    mutated.loc[future, "open"] = noise
    mutated.loc[future, "close"] = noise * rng.uniform(0.5, 1.5, n)
    mutated.loc[future, "high"] = mutated.loc[future, ["open", "close"]].max(axis=1) * 1.1
    mutated.loc[future, "low"] = mutated.loc[future, ["open", "close"]].min(axis=1) * 0.9
    mutated.loc[future, "volume"] = rng.uniform(0, 1e6, n)

    assert snap.snapshot(mutated, t, HOUR, PARAMS) == snap.snapshot(FULL, t, HOUR, PARAMS)
    # The precomputed feature table (what the backtester will use) must be causal row by row.
    before = snap.features(FULL, HOUR, PARAMS).loc[:t]
    after = snap.features(mutated, HOUR, PARAMS).loc[:t]
    pd.testing.assert_frame_equal(before, after, check_exact=True)


@pytest.mark.parametrize("t", [T0 + 201 * HOUR, T0 + 450 * HOUR, T0 + 799 * HOUR])
def test_mutating_the_past_does_change_the_snapshot(t: pd.Timestamp) -> None:
    """Sensitivity check: proves the look-ahead tests above are able to fail."""
    last_closed = FULL.index[FULL["timestamp"] + HOUR == t][0]
    mutated = FULL.copy()
    mutated.loc[last_closed, ["high", "close"]] *= 1.05
    assert snap.snapshot(mutated, t, HOUR, PARAMS) != snap.snapshot(FULL, t, HOUR, PARAMS)
    before = snap.features(FULL, HOUR, PARAMS).loc[:t]
    after = snap.features(mutated, HOUR, PARAMS).loc[:t]
    assert not before.equals(after)


def test_snapshot_time_is_close_time_of_last_closed_candle() -> None:
    t = T0 + 450 * HOUR + pd.Timedelta(minutes=30)  # candle opened at T0+450h is still forming
    s = snap.snapshot(FULL, t, HOUR, PARAMS)
    assert s is not None
    assert s.time == T0 + 450 * HOUR
    assert s.close == FULL["close"].iloc[449]


# --- determinism and warmup -----------------------------------------------------------------


def test_deterministic_byte_identical() -> None:
    a = snap.features(FULL, HOUR, PARAMS)
    b = snap.features(FULL.copy(), HOUR, PARAMS)
    cols = list(snap.FEATURES)
    assert a[cols].to_numpy(dtype="float64").tobytes() == b[cols].to_numpy("float64").tobytes()
    assert a["ready"].to_numpy().tobytes() == b["ready"].to_numpy().tobytes()
    assert a.index.equals(b.index)
    assert repr(snap.snapshot(FULL, PROBES[-1], HOUR, PARAMS)) == repr(
        snap.snapshot(FULL.copy(), PROBES[-1], HOUR, PARAMS)
    )


def test_warmup() -> None:
    assert PARAMS.warmup_bars(HOUR) == 200
    feats = snap.features(FULL, HOUR, PARAMS)
    assert not feats["ready"].iloc[:199].any()
    assert feats["ready"].iloc[199:].all()
    assert snap.snapshot(FULL, T0 + 199 * HOUR, HOUR, PARAMS) is None
    ready = snap.snapshot(FULL, T0 + 200 * HOUR, HOUR, PARAMS)
    assert ready is not None
    assert all(math.isfinite(v) for v in vars(ready).values() if isinstance(v, float))
    assert snap.snapshot(FULL, T0, HOUR, PARAMS) is None  # no closed candles at all


def test_no_nan_in_ready_rows() -> None:
    feats = snap.features(FULL, HOUR, PARAMS)
    assert not feats[feats["ready"]].isna().any().any()


# --- indicator reference values --------------------------------------------------------------


def test_ema_hand_computed() -> None:
    # span 3 -> alpha 0.5, seeded with the first value
    assert snap.ema(pd.Series([1.0, 2.0, 3.0, 4.0]), 3).tolist() == [1.0, 1.5, 2.25, 3.125]


def test_wilder_atr_hand_computed() -> None:
    high = pd.Series([10.0, 11.0, 12.0, 11.0, 13.0])
    low = pd.Series([9.0, 10.0, 10.0, 9.0, 11.0])
    close = pd.Series([9.5, 10.5, 11.0, 10.0, 12.5])
    # TR: 1 (h-l, no prev), max(1, 1.5, .5)=1.5, max(2, 1.5, .5)=2, max(2, 0, 2)=2, max(2, 3, 1)=3
    atr = snap.wilder_atr(high, low, close, 3)
    assert math.isnan(atr[0]) and math.isnan(atr[1])
    assert atr[2] == pytest.approx(1.5)  # SMA seed of first 3 TRs
    assert atr[3] == pytest.approx((1.5 * 2 + 2) / 3)
    assert atr[4] == pytest.approx(((1.5 * 2 + 2) / 3 * 2 + 3) / 3)


def test_feature_definitions() -> None:
    feats = snap.features(FULL, HOUR, PARAMS)
    c = FULL["close"].to_numpy()
    i = 500
    row = feats.iloc[i]
    assert row["ret_24h"] == pytest.approx(c[i] / c[i - 24] - 1)
    assert row["ret_7d"] == pytest.approx(c[i] / c[i - 168] - 1)
    assert row["atr_pct"] == pytest.approx(100 * row["atr"] / c[i])
    assert row["drawdown"] == pytest.approx(c[i] / c[i - 167 : i + 1].max() - 1)
    log_r = np.diff(np.log(c[i - 168 : i + 1]))
    assert row["realized_vol"] == pytest.approx(log_r.std(ddof=1) * math.sqrt(24 * 365))
