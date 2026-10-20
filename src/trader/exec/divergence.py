"""Divergence monitor: does paper trading do what the backtest engine would have done?

Replays the engine over the same 4h bars since paper start and compares entries and exits
bar by bar. Prices may differ (paper fills walk the real order book) but signals and
trade times must match exactly. Comparison stops at the first kill-switch flatten, after
which paper deliberately departs from the strategy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from trader.backtest.costs import Costs
from trader.backtest.engine import EngineConfig, Result, run
from trader.exec.journal import PositionRow
from trader.risk.limits import RiskLimits
from trader.strategy.base import Strategy

STRATEGY_EXITS = ("signal", "stop")


def replay(
    bars: dict[str, pd.DataFrame],
    feats: dict[str, pd.DataFrame],
    strategy: Strategy,
    costs: Costs,
    limits: RiskLimits,
    initial_equity: float,
    paper_start: pd.Timestamp,
    last_close: pd.Timestamp,
    step: pd.Timedelta,
) -> Result:
    """Engine run whose first decision is at the close paper started on (`paper_start`)."""
    cfg = EngineConfig(initial_equity, costs, True, limits)
    return run(bars, feats, strategy, cfg, paper_start - step, last_close, step)


def compare(
    paper_trades: pd.DataFrame,
    paper_positions: Mapping[str, PositionRow],
    engine: Result,
    horizon: pd.Timestamp,
    step: pd.Timedelta,
    paper_fills: pd.DataFrame,
) -> list[str]:
    """Mismatched entries/exits, keyed by bar open time, for bars before `horizon`."""
    flattened = paper_fills.loc[paper_fills["reason"] == "killswitch", "time"]
    if not flattened.empty:
        horizon = min(horizon, flattened.min().floor(step))

    def bar(t: Any) -> pd.Timestamp:
        return pd.Timestamp(t).floor(step)

    paper_entries = {(t.symbol, bar(t.entry_time)) for t in paper_trades.itertuples()}
    paper_entries |= {(p.symbol, bar(p.entry_time)) for p in paper_positions.values()}
    paper_exits = {
        (t.symbol, bar(t.entry_time), bar(t.exit_time), t.exit_reason)
        for t in paper_trades.itertuples()
        if t.exit_reason in STRATEGY_EXITS
    }
    e = engine.trades
    engine_entries = {(t.symbol, bar(t.entry_time)) for t in e.itertuples()}
    engine_exits = {
        (t.symbol, bar(t.entry_time), bar(t.exit_time), t.exit_reason)
        for t in e.itertuples()
        if t.exit_reason in STRATEGY_EXITS
    }
    paper_entries = {x for x in paper_entries if x[1] < horizon}
    engine_entries = {x for x in engine_entries if x[1] < horizon}
    paper_exits = {x for x in paper_exits if x[2] < horizon}
    engine_exits = {x for x in engine_exits if x[2] < horizon}
    out = [f"entry only in paper: {x}" for x in sorted(paper_entries - engine_entries)]
    out += [f"entry only in backtest: {x}" for x in sorted(engine_entries - paper_entries)]
    out += [f"exit only in paper: {x}" for x in sorted(paper_exits - engine_exits)]
    out += [f"exit only in backtest: {x}" for x in sorted(engine_exits - paper_exits)]
    return out


def slippage_check(
    fills: pd.DataFrame, model_slippage: float, min_fills: int, mult: float
) -> tuple[int, float, bool]:
    """Average realised slippage of market fills vs their reference price. Warn when it
    exceeds `mult` x the model over at least `min_fills` fills. Simulated stop fills are
    excluded (their slippage is the model by construction)."""
    m = fills[fills["reason"].isin(["entry", "signal", "killswitch"])]
    if m.empty:
        return 0, 0.0, False
    side = m["side"].map({"buy": 1.0, "sell": -1.0})
    slip = side * (m["price"] / m["raw_price"] - 1)
    avg = float(slip.mean())
    return len(m), avg, len(m) >= min_fills and avg > mult * model_slippage
