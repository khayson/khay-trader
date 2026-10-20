"""Performance metrics. Crypto trades 24/7: an hourly curve has 8760 periods a year."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

PERIODS_PER_YEAR = 24 * 365


def max_drawdown(equity: pd.Series) -> float:
    """Worst peak-to-trough decline as a negative fraction (0.0 if none)."""
    return float((equity / equity.cummax() - 1).min())


def sharpe(equity: pd.Series, periods_per_year: float = PERIODS_PER_YEAR) -> float:
    """Annualized Sharpe of per-bar returns (risk-free rate 0)."""
    r = equity.pct_change().dropna()
    std = r.std()
    if not std > 0:
        return 0.0
    return float(r.mean() / std * math.sqrt(periods_per_year))


def cagr(total_return: float, duration: pd.Timedelta) -> float:
    years = duration / pd.Timedelta(days=365.25)
    return float((1 + total_return) ** (1 / years) - 1)


def trade_stats(returns: pd.Series, pnls: pd.Series) -> dict[str, float]:
    n = len(returns)
    if n == 0:
        return {"trades": 0, "win_rate": math.nan, "profit_factor": math.nan, "avg_trade": math.nan}
    gross_loss = -pnls[pnls < 0].sum()
    return {
        "trades": n,
        "win_rate": float((pnls > 0).mean()),
        "profit_factor": float(pnls[pnls > 0].sum() / gross_loss) if gross_loss > 0 else math.inf,
        "avg_trade": float(returns.mean()),
    }


def bootstrap_ci(
    returns: np.ndarray | pd.Series, seed: int, n_boot: int = 10_000, level: float = 0.95
) -> tuple[float, float]:
    """Percentile bootstrap interval for the mean trade return."""
    r = np.asarray(returns, dtype=float)
    rng = np.random.default_rng(seed)
    means = r[rng.integers(0, len(r), size=(n_boot, len(r)))].mean(axis=1)
    tail = (1 - level) / 2 * 100
    return float(np.percentile(means, tail)), float(np.percentile(means, 100 - tail))
