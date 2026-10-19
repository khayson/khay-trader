from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from trader.backtest import research
from trader.backtest.costs import Costs
from trader.backtest.engine import EngineConfig, run
from trader.backtest.report import Meta
from trader.state import snapshot as snap
from trader.strategy.base import StopSizing

HOUR = pd.Timedelta(hours=1)
T0 = pd.Timestamp("2018-01-01", tz="UTC")
ROOT = Path(__file__).parent.parent
CONFIG = yaml.safe_load((ROOT / "config" / "default.yaml").read_text())
SIZING = StopSizing(3.0, 0.5, 10)


def score(net: float, pct: float, sharpe: float = 1.0, dd: float = -0.05) -> research.SplitScore:
    return research.SplitScore(net_return=net, percentile=pct, sharpe=sharpe, max_dd=dd)


# --- pre-registered decision rule ------------------------------------------------------------


def test_advance_requires_both_splits_positive_and_80th_percentile() -> None:
    scores = {
        "A": {"train": score(0.1, 85), "validate": score(0.05, 79.9)},  # validate percentile low
        "B": {"train": score(-0.01, 95), "validate": score(0.2, 99)},  # train net negative
        "C": {"train": score(0.1, 80), "validate": score(0.01, 80, sharpe=0.5)},
        "D": {"train": score(0.1, 90), "validate": score(0.02, 90, sharpe=0.7)},
    }
    assert research.advancing(scores, min_percentile=80) == "D"  # highest validate Sharpe
    del scores["C"], scores["D"]
    assert research.advancing(scores, min_percentile=80) is None


def test_go_rule() -> None:
    assert research.go(score(0.01, 80, dd=-0.15), 80, 15)
    assert not research.go(score(0.01, 80, dd=-0.151), 80, 15)
    assert not research.go(score(0.0, 99), 80, 15)
    assert not research.go(score(0.1, 79), 80, 15)


# --- random-entry schedule and Monte Carlo through the engine --------------------------------


def test_random_schedule_is_non_overlapping_and_seeded() -> None:
    times = pd.date_range(T0, periods=200, freq="1h")
    holds = np.array([5, 10, 1, 30])
    entries, exits = research.random_schedule(times, holds, np.random.default_rng(3))
    again = research.random_schedule(times, holds, np.random.default_rng(3))
    assert (entries, exits) == again
    e, x = sorted(entries), sorted(exits)
    assert len(e) == len(x) == 4
    assert sorted(int((b - a) / HOUR) for a, b in zip(e, x, strict=True)) == [1, 5, 10, 30]
    assert all(x[j] < e[j + 1] for j in range(3))  # strictly after the previous exit
    assert set(entries) | set(exits) <= set(times)


def test_random_schedule_rejects_holds_longer_than_window() -> None:
    times = pd.date_range(T0, periods=10, freq="1h")
    with pytest.raises(ValueError):
        research.random_schedule(times, np.array([6, 6]), np.random.default_rng(0))


def test_random_entry_runs_through_shared_pool_engine() -> None:
    rng = np.random.default_rng(0)
    n = 1500
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, n)))
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range(T0, periods=n, freq="1h"),
            "open": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": 1.0,
        }
    )
    params = snap.FeatureParams.from_config(CONFIG)
    feats = snap.features(df, HOUR, params)
    times = pd.DatetimeIndex(feats.index[feats["ready"]][:-1])
    entries, exits = research.random_schedule(times, np.array([10, 20, 5]), rng)
    strat = research.RandomEntry(entries, exits, SIZING)
    cfg = EngineConfig(10_000.0, Costs(0.001, 0.0005), True, 10, 20)
    res = run({"X/USDT": df}, {"X/USDT": feats}, strat, cfg, T0, T0 + n * HOUR, HOUR)
    assert sorted(res.trades["bars_held"]) == [5, 10, 20]
    assert (res.trades["exit_reason"] == "signal").all()


# --- full report on synthetic data -----------------------------------------------------------


def synthetic(seed: int, n: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.00005, 0.006, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    spread = np.abs(rng.normal(0, 0.003, n)) * close
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(T0, periods=n, freq="1h"),
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": 1.0,
        }
    )


PROTOCOL = yaml.safe_load((ROOT / "config" / "research_round1.yaml").read_text())
PROTOCOL = {
    **PROTOCOL,
    "mc_runs": 4,
    "splits": {
        "train": ["2018-01-01", "2019-07-01"],
        "validate": ["2019-07-01", "2020-04-01"],
        "test": ["2020-04-01", None],
    },
}
CANDLES = {"BTC/USDT": synthetic(1, 24 * 900), "ETH/USDT": synthetic(2, 24 * 900)}
META = Meta(
    date="2026-01-01",
    git_commit="abc123",
    config_sha256="c" * 64,
    data_sha256={"BTC/USDT": "a" * 64, "ETH/USDT": "b" * 64},
    seed=42,
)


def test_research_report_deterministic_and_test_stays_locked() -> None:
    md1, trades1 = research.build_research_report(CANDLES, CONFIG, PROTOCOL, {}, META, False)
    md2, trades2 = research.build_research_report(CANDLES, CONFIG, PROTOCOL, {}, META, False)
    assert md1 == md2
    assert trades1.keys() == trades2.keys() == {"A", "B", "C", "D"}
    for name in trades1:
        pd.testing.assert_frame_equal(trades1[name], trades2[name])
        assert (trades1[name]["split"] != "test").all()
    for needle in ["abc123", "seed: 42", "Multiple comparisons", "## Decision", "LOCKED"]:
        assert needle in md1, needle
    assert "## Test" not in md1
