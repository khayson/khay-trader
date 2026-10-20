"""Divergence monitor: does paper trading do what the backtest engine would have done?

Replays the engine over the same 4h bars since paper start and compares entries and exits
bar by bar. Prices may differ (paper fills walk the real order book) but signals and
trade times must match. Comparison stops at the first kill-switch flatten, after which
paper deliberately departs from the strategy.

A mismatch is *explained* when an unclean bar precedes it in the same position episode:
a missed, late or stale wake, a runtime outage, a data revision (the replay's features for
a decision bar differ from those recorded when the decision was made), or an entry the
paper loop declined for an operational reason. "Same episode" means there is no bar
between the two where both paper and replay were flat in that symbol, because the states
can only differ until both are flat again. Explained mismatches are counted and reported;
unexplained ones trip the kill switch.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from trader.backtest.costs import Costs
from trader.backtest.engine import EngineConfig, Result, run
from trader.exec.journal import PositionRow
from trader.risk.limits import RiskLimits
from trader.strategy.base import Strategy

STRATEGY_EXITS = ("signal", "stop")


def _float(value: Any) -> float:
    return float(value)


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


def revised_bars(
    decisions: pd.DataFrame, feats: Mapping[str, pd.DataFrame], rel_tol: float = 1e-9
) -> set[pd.Timestamp]:
    """Decision bars whose recorded features differ from the replay's features now."""
    revised = set()
    for symbol, bar_close, raw in zip(
        decisions["symbol"], decisions["bar_close"], decisions["features"], strict=True
    ):
        recorded = json.loads(raw) if isinstance(raw, str) else None
        if not recorded:
            continue
        f = feats.get(str(symbol))
        t = pd.Timestamp(bar_close)
        if f is None or t not in f.index:
            revised.add(t)
            continue
        for name, value in recorded.items():
            now = _float(f.at[t, name])
            if not math.isclose(now, float(value), rel_tol=rel_tol, abs_tol=1e-12):
                revised.add(t)
                break
    return revised


def compare(
    paper_trades: pd.DataFrame,
    paper_positions: Mapping[str, PositionRow],
    engine: Result,
    horizon: pd.Timestamp,
    step: pd.Timedelta,
    paper_fills: pd.DataFrame,
    unclean: Iterable[pd.Timestamp] = (),
) -> tuple[list[str], list[str]]:
    """(unexplained, explained) entry/exit mismatches for bars before `horizon`."""
    flattened = paper_fills.loc[paper_fills["reason"] == "killswitch", "time"]
    if not flattened.empty:
        horizon = min(horizon, pd.Timestamp(flattened.min()).floor(step))

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

    # Held intervals [entry bar, exit bar] per symbol, for both sides.
    held: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for t in paper_trades.itertuples():
        held.setdefault(str(t.symbol), []).append((bar(t.entry_time), bar(t.exit_time)))
    for p in paper_positions.values():
        held.setdefault(p.symbol, []).append((bar(p.entry_time), horizon))
    for t in e.itertuples():
        exit_bar = horizon if t.exit_reason == "end" else bar(t.exit_time)
        held.setdefault(str(t.symbol), []).append((bar(t.entry_time), exit_bar))

    dirty = sorted(set(unclean))

    def explained(symbol: str, at: pd.Timestamp) -> bool:
        for u in dirty:
            if u > at:
                break
            t = u + step
            flat_between = False
            while t < at:
                if not any(a <= t <= b for a, b in held.get(symbol, [])):
                    flat_between = True
                    break
                t += step
            if not flat_between:
                return True
        return False

    found: list[tuple[str, pd.Timestamp, str]] = []
    found += [
        (str(s), b, f"entry only in paper: {(s, b)}")
        for s, b in sorted(paper_entries - engine_entries)
        if b < horizon
    ]
    found += [
        (str(s), b, f"entry only in backtest: {(s, b)}")
        for s, b in sorted(engine_entries - paper_entries)
        if b < horizon
    ]
    found += [
        (str(x[0]), x[2], f"exit only in paper: {x}")
        for x in sorted(paper_exits - engine_exits)
        if x[2] < horizon
    ]
    found += [
        (str(x[0]), x[2], f"exit only in backtest: {x}")
        for x in sorted(engine_exits - paper_exits)
        if x[2] < horizon
    ]
    unexplained = [m for s, b, m in found if not explained(s, b)]
    explained_list = [m for s, b, m in found if explained(s, b)]
    return unexplained, explained_list


@dataclass(frozen=True)
class SlippageReport:
    fills: int
    total: float  # vs the bar-open reference: what the 0.05% model represents
    impact: float | None  # fill vs book mid at fill time (execution cost)
    drift: float | None  # book mid vs the bar-open reference (price moved meanwhile)
    warn: bool

    def describe(self, model: float) -> str:
        def pct(x: float | None) -> str:
            return "n/a" if x is None else f"{x:.4%}"

        return (
            f"{self.fills} market fills: average slippage {pct(self.total)} vs model "
            f"{model:.4%} (impact vs mid {pct(self.impact)}, drift vs open "
            f"{pct(self.drift)})"
        )


def slippage_check(
    fills: pd.DataFrame, model_slippage: float, min_fills: int, mult: float
) -> SlippageReport:
    """Average realised slippage of market fills. Warn when the total exceeds `mult` x the
    model over at least `min_fills` fills. Simulated stop fills are excluded (their slippage
    is the model by construction)."""
    m = fills[fills["reason"].isin(["entry", "signal", "killswitch"])]
    if m.empty:
        return SlippageReport(0, 0.0, None, None, False)
    side = m["side"].map({"buy": 1.0, "sell": -1.0})
    total = float((side * (m["price"] / m["raw_price"] - 1)).mean())
    impact = drift = None
    if "mid" in m and m["mid"].notna().any():
        with_mid = m[m["mid"].notna()]
        s = side[with_mid.index]
        impact = float((s * (with_mid["price"] / with_mid["mid"] - 1)).mean())
        drift = float((s * (with_mid["mid"] / with_mid["raw_price"] - 1)).mean())
    return SlippageReport(
        len(m), total, impact, drift, len(m) >= min_fills and total > mult * model_slippage
    )
