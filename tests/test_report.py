from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from trader.backtest import report

T0 = pd.Timestamp("2024-01-01", tz="UTC")
CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config" / "default.yaml").read_text())
META = report.Meta(
    date="2026-01-01",
    git_commit="abc123",
    config_sha256="c" * 64,
    data_sha256={"A/USDT": "a" * 64, "B/USDT": "b" * 64},
    seed=42,
)


def walk(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.008, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    spread = np.abs(rng.normal(0, 0.004, n)) * close
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


CANDLES = {"A/USDT": walk(14_000, 1), "B/USDT": walk(14_000, 2)}
CFG = {**CONFIG, "pairs": ["A/USDT", "B/USDT"]}


def test_splits_are_contiguous_12_6_rest() -> None:
    end = T0 + pd.Timedelta(days=730)
    train, validate, test = report.make_splits(T0, end)
    assert (train.start, train.end) == (T0, pd.Timestamp("2025-01-01", tz="UTC"))
    assert (validate.start, validate.end) == (train.end, pd.Timestamp("2025-07-01", tz="UTC"))
    assert (test.start, test.end) == (validate.end, end)


def test_report_is_deterministic_and_reproducible() -> None:
    md1, trades1 = report.build_report(CANDLES, CFG, META, include_test=True, n_mc=20)
    md2, trades2 = report.build_report(CANDLES, CFG, META, include_test=True, n_mc=20)
    assert md1 == md2
    for v in trades1:
        pd.testing.assert_frame_equal(trades1[v], trades2[v])
    for needle in ["abc123", "c" * 64, "a" * 64, "b" * 64, "seed: 42", "## train", "## test"]:
        assert needle in md1, needle


def test_test_split_stays_locked_by_default() -> None:
    md, trades = report.build_report(CANDLES, CFG, META, include_test=False, n_mc=5)
    assert "## test" not in md
    assert "LOCKED" in md
    assert all((t["split"] != "test").all() for t in trades.values())


def test_random_entry_matches_holding_times_and_is_seeded() -> None:
    opens = np.linspace(100, 200, 500)
    trades = pd.DataFrame(
        {"bars_held": [10, 20, 30], "entry_cost": [100.0] * 3, "entry_equity": [1000.0] * 3}
    )
    a = report.random_entry_returns(opens, trades, report.Costs(0, 0), np.random.default_rng(1), 50)
    b = report.random_entry_returns(opens, trades, report.Costs(0, 0), np.random.default_rng(1), 50)
    np.testing.assert_array_equal(a, b)
    # Rising prices, no costs: every random long trade wins.
    assert (a > 0).all()
