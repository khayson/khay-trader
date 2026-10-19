import math

import numpy as np
import pandas as pd
import pytest

from trader.backtest import metrics

T0 = pd.Timestamp("2024-01-01", tz="UTC")


def series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.date_range(T0, periods=len(values), freq="1h"))


def test_max_drawdown() -> None:
    assert metrics.max_drawdown(series([100, 120, 90, 130, 117])) == pytest.approx(-0.25)
    assert metrics.max_drawdown(series([1, 2, 3])) == 0.0


def test_sharpe_hourly_annualized() -> None:
    eq = series([100, 101, 100, 101, 100])
    r = eq.pct_change().dropna()
    assert metrics.sharpe(eq) == pytest.approx(r.mean() / r.std() * math.sqrt(8760))
    assert metrics.sharpe(series([100, 100, 100])) == 0.0


def test_cagr() -> None:
    assert metrics.cagr(0.21, pd.Timedelta(days=2 * 365.25)) == pytest.approx(0.1)


def test_trade_stats() -> None:
    stats = metrics.trade_stats(pd.Series([0.1, -0.05, 0.02]), pd.Series([100.0, -50.0, 20.0]))
    assert stats["trades"] == 3
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["profit_factor"] == pytest.approx(120 / 50)
    assert stats["avg_trade"] == pytest.approx(0.07 / 3)
    empty = metrics.trade_stats(pd.Series([], dtype=float), pd.Series([], dtype=float))
    assert empty["trades"] == 0 and math.isnan(empty["win_rate"])


def test_bootstrap_ci_brackets_mean_and_is_seeded() -> None:
    r = np.random.default_rng(0).normal(0.01, 0.02, 200)
    lo, hi = metrics.bootstrap_ci(r, seed=5)
    assert lo < r.mean() < hi
    assert (lo, hi) == metrics.bootstrap_ci(r, seed=5)
