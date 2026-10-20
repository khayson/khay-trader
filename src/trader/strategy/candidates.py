"""Research round 1 candidates (pre-registered in config/research_round1.yaml)."""

from __future__ import annotations

from trader.state.snapshot import Snapshot
from trader.strategy.base import StopSizing, TrailingStopStrategy


class EmaTrend(TrailingStopStrategy):
    """Enter when close > EMA fast > EMA slow; exit when close < EMA slow."""

    def __init__(self, sizing: StopSizing) -> None:
        self.sizing = sizing

    def entry_signal(self, snap: Snapshot) -> bool:
        return snap.close > snap.ema_fast > snap.ema_slow

    def exit_signal(self, snap: Snapshot) -> bool:
        return snap.close < snap.ema_slow


class Breakout(TrailingStopStrategy):
    """Enter when close > prior entry-channel high; exit when close < prior exit-channel low."""

    def __init__(self, sizing: StopSizing) -> None:
        self.sizing = sizing

    def entry_signal(self, snap: Snapshot) -> bool:
        return snap.close > snap.channel_high

    def exit_signal(self, snap: Snapshot) -> bool:
        return snap.close < snap.channel_low
