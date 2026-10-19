from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from trader.state import snapshot as snap
from trader.state.snapshot import FeatureParams, Snapshot
from trader.strategy.base import Action, Intent, Position
from trader.strategy.baseline import Baseline, BaselineParams

HOUR = pd.Timedelta(hours=1)
T = pd.Timestamp("2024-06-01", tz="UTC")
CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config" / "default.yaml").read_text())
PARAMS = BaselineParams(
    atr_pct_min=0.2, atr_pct_max=3.0, stop_atr_mult=3.0, risk_per_trade_pct=0.5, max_position_pct=10
)
STRAT = Baseline(PARAMS)


def make_snap(
    close: float = 100.0, ema_fast: float = 95.0, ema_slow: float = 90.0, atr: float = 1.0
) -> Snapshot:
    return Snapshot(
        time=T,
        close=close,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        atr=atr,
        atr_pct=100 * atr / close,
        ret_24h=0.0,
        ret_7d=0.0,
        realized_vol=0.5,
        drawdown=0.0,
    )


def test_params_load_from_config() -> None:
    assert BaselineParams.from_config(CONFIG) == PARAMS


def test_uptrend_enters_with_risk_based_size() -> None:
    intent = STRAT.decide(make_snap(atr=0.5), None, equity=100_000)
    assert intent.action is Action.ENTER
    assert intent.stop_price == pytest.approx(98.5)
    # risk 500 / stop distance 1.5 = 333 units = 33k notional > 10k cap -> capped
    assert intent.size == pytest.approx(100.0)


def test_size_uses_risk_when_under_cap() -> None:
    # ATR 2.5 -> stop distance 7.5 -> 500 / 7.5 = 66.7 units = 6.7k notional < 10k cap
    intent = STRAT.decide(make_snap(atr=2.5), None, equity=100_000)
    assert intent.action is Action.ENTER
    assert intent.size == pytest.approx(500 / 7.5)
    assert intent.size * 100 <= 0.10 * 100_000


@pytest.mark.parametrize(
    "s",
    [
        make_snap(close=89.0),  # close below EMA200
        make_snap(ema_fast=89.0),  # EMA50 below EMA200
        make_snap(close=93.0),  # between EMA200 and EMA50: would churn
        make_snap(atr=0.1),  # ATR% 0.1 < band
        make_snap(atr=4.0),  # ATR% 4 > band
    ],
    ids=["below_ema200", "ema50_below_ema200", "below_ema50", "atr_too_low", "atr_too_high"],
)
def test_entry_blocked(s: Snapshot) -> None:
    assert STRAT.decide(s, None, equity=100_000) == Intent(Action.HOLD)


def test_no_equity_no_entry() -> None:
    assert STRAT.decide(make_snap(), None, equity=0.0).action is Action.HOLD


def test_trend_break_exits() -> None:
    pos = Position(qty=10, entry_price=100, stop_price=80)
    intent = STRAT.decide(make_snap(close=94.0), pos, equity=100_000)  # close < EMA50 (95)
    assert intent == Intent(Action.EXIT, size=10)


def test_stop_hit_exits() -> None:
    pos = Position(qty=10, entry_price=100, stop_price=100.5)
    assert STRAT.decide(make_snap(close=100.0), pos, equity=100_000) == Intent(Action.EXIT, size=10)


def test_hold_ratchets_trailing_stop_up_never_down() -> None:
    pos = Position(qty=10, entry_price=100, stop_price=96)
    up = STRAT.decide(make_snap(close=105.0, atr=1.0), pos, equity=100_000)
    assert up == Intent(Action.HOLD, stop_price=102.0)
    down = STRAT.decide(make_snap(close=99.0, atr=1.0), pos, equity=100_000)
    assert down == Intent(Action.HOLD, stop_price=96.0)


def test_clear_uptrend_series_enters_end_to_end() -> None:
    n = 400
    close = 100 * np.exp(np.linspace(0, 0.4, n)) * (1 + 0.003 * np.sin(np.arange(n)))
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range(T, periods=n, freq="1h"),
            "open": close,
            "high": close * 1.004,
            "low": close * 0.996,
            "close": close,
            "volume": 1.0,
        }
    )
    s = snap.snapshot(candles, T + n * HOUR, HOUR, FeatureParams.from_config(CONFIG))
    assert s is not None
    assert STRAT.decide(s, None, equity=10_000).action is Action.ENTER
