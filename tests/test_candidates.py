import pandas as pd
import pytest

from trader.state.snapshot import Snapshot
from trader.strategy.base import Action, Intent, Position, StopSizing
from trader.strategy.candidates import Breakout, EmaTrend

SIZING = StopSizing(stop_atr_mult=3.0, risk_per_trade_pct=0.5, max_position_pct=10)


def make_snap(
    close: float = 100.0,
    ema_fast: float = 95.0,
    ema_slow: float = 90.0,
    atr: float = 1.0,
    channel_high: float = 99.0,
    channel_low: float = 92.0,
) -> Snapshot:
    return Snapshot(
        time=pd.Timestamp("2024-06-01", tz="UTC"),
        close=close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        atr=atr,
        atr_pct=100 * atr / close,
        ret_24h=0.0,
        ret_7d=0.0,
        realized_vol=0.5,
        drawdown=0.0,
        channel_high=channel_high,
        channel_low=channel_low,
    )


HELD = Position(qty=10, entry_price=100, stop_price=80)


def test_ema_trend_entry_needs_close_above_fast_above_slow() -> None:
    s = EmaTrend(SIZING)
    entry = s.decide(make_snap(), None, 100_000)
    assert entry == Intent(Action.ENTER, size=100.0, stop_price=97.0)  # capped at 10% notional
    assert s.decide(make_snap(close=94), None, 100_000).action is Action.HOLD
    assert s.decide(make_snap(ema_fast=89), None, 100_000).action is Action.HOLD


def test_ema_trend_exits_below_slow_not_fast() -> None:
    s = EmaTrend(SIZING)
    assert s.decide(make_snap(close=93), HELD, 100_000).action is Action.HOLD  # below fast only
    assert s.decide(make_snap(close=89), HELD, 100_000) == Intent(Action.EXIT, size=10)


def test_breakout_entry_and_exit() -> None:
    s = Breakout(SIZING)
    assert s.decide(make_snap(close=100, channel_high=99), None, 100_000).action is Action.ENTER
    assert s.decide(make_snap(close=99, channel_high=99), None, 100_000).action is Action.HOLD
    assert s.decide(make_snap(close=93, channel_low=92), HELD, 100_000).action is Action.HOLD
    assert s.decide(make_snap(close=91, channel_low=92), HELD, 100_000).action is Action.EXIT


@pytest.mark.parametrize("strategy", [EmaTrend(SIZING), Breakout(SIZING)])
def test_atr_stop_ratchets_and_exits(strategy: EmaTrend | Breakout) -> None:
    pos = Position(qty=10, entry_price=100, stop_price=96)
    assert strategy.decide(make_snap(close=105), pos, 100_000) == Intent(
        Action.HOLD, stop_price=102.0
    )
    stopped = Position(qty=10, entry_price=100, stop_price=100.5)
    assert strategy.decide(make_snap(close=100), stopped, 100_000).action is Action.EXIT
