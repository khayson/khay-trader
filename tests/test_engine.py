from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from trader.backtest.costs import ZERO, Costs
from trader.backtest.engine import EngineConfig, Result, run
from trader.risk.limits import RiskLimits
from trader.state import snapshot as snap
from trader.state.snapshot import FeatureParams, Snapshot
from trader.strategy.base import Action, Intent, Position
from trader.strategy.baseline import Baseline, BaselineParams

HOUR = pd.Timedelta(hours=1)
T0 = pd.Timestamp("2024-01-01", tz="UTC")
COSTS = Costs(fee=0.001, slippage=0.0005)
TOY_PARAMS = FeatureParams(
    ema_fast=2,
    ema_slow=3,
    atr_period=2,
    vol_window_hours=24,
    drawdown_window_hours=24,
    channel_entry=2,
    channel_exit=2,
)  # warmup 169 bars (7d return)
CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config" / "default.yaml").read_text())
SYM = "X/USDT"


def loose(max_pos: float = 100, max_total: float = 100) -> RiskLimits:
    """Caps only: daily-loss and order-rate limits far out of the way."""
    return RiskLimits(
        max_pos, max_total, max_daily_loss_pct=100, max_orders_per_hour=1000, stale_data_seconds=0
    )


def candles(
    open_: list[float] | np.ndarray,
    close: list[float] | np.ndarray,
    low: list[float] | np.ndarray | None = None,
) -> pd.DataFrame:
    o, c = np.asarray(open_, float), np.asarray(close, float)
    lo = np.minimum(o, c) if low is None else np.asarray(low, float)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(T0, periods=len(o), freq="1h"),
            "open": o,
            "high": np.maximum(o, c),
            "low": lo,
            "close": c,
            "volume": 1.0,
        }
    )


class Script:
    """Enters/exits on given signal-candle indices (index i = candle opened at T0 + i hours)."""

    def __init__(self, enter: dict[int, tuple[float, float]], exit_: set[int]):
        self.enter, self.exit = enter, exit_

    def decide(self, s: Snapshot, position: Position | None, equity: float) -> Intent:
        i = int((s.time - T0) / HOUR) - 1
        if position is None and i in self.enter:
            size, stop = self.enter[i]
            return Intent(Action.ENTER, size=size, stop_price=stop)
        if position is not None and i in self.exit:
            return Intent(Action.EXIT, size=position.qty)
        return Intent(Action.HOLD, stop_price=position.stop_price if position else None)


def run_one(
    df: pd.DataFrame,
    strategy: object,
    costs: Costs = COSTS,
    intrabar: bool = True,
    max_pos: float = 100,
    max_total: float = 100,
) -> Result:
    cfg = EngineConfig(10_000.0, costs, intrabar, loose(max_pos, max_total))
    feats = {SYM: snap.features(df, HOUR, TOY_PARAMS)}
    start, end = df["timestamp"].iloc[0], df["timestamp"].iloc[-1] + HOUR
    return run({SYM: df}, feats, strategy, cfg, start, end, HOUR)  # type: ignore[arg-type]


# --- toy series ------------------------------------------------------------------------------


def test_flat_price_loses_exactly_round_trip_costs() -> None:
    flat = [100.0] * 200
    res = run_one(candles(flat, flat), Script({180: (10, 50)}, {185}))
    expected = 10_000 - 10 * 100 * ((1.0005) * (1.001) - (0.9995) * (0.999))
    assert res.equity["equity"].iloc[-1] == pytest.approx(expected, rel=1e-12)
    assert len(res.trades) == 1
    assert res.trades["pnl"].iloc[0] == pytest.approx(expected - 10_000, rel=1e-9)


@pytest.mark.parametrize("costs", [ZERO, COSTS])
def test_steady_rise_hand_computed_profit(costs: Costs) -> None:
    o = 100 + np.arange(200.0)
    res = run_one(candles(o, o + 1), Script({170: (10, 1)}, {180}), costs=costs)
    # entry fills at open of candle 171 (=271), exit at open of candle 181 (=281)
    buy = 271 * (1 + costs.slippage)
    sell = 281 * (1 - costs.slippage)
    expected = 10 * sell * (1 - costs.fee) - 10 * buy * (1 + costs.fee)
    assert res.trades["pnl"].iloc[0] == pytest.approx(expected, rel=1e-12)
    assert res.equity["equity"].iloc[-1] == pytest.approx(10_000 + expected, rel=1e-12)


def test_gap_down_through_stop_fills_at_open() -> None:
    o = np.full(200, 100.0)
    c = o.copy()
    lo = o.copy()
    o[191], c[191], lo[191] = 90.0, 88.0, 85.0
    res = run_one(candles(o, c, lo), Script({180: (10, 95)}, set()))
    stop = res.fills[res.fills["reason"] == "stop"]
    assert len(stop) == 1
    assert stop["price"].iloc[0] == pytest.approx(90 * (1 - COSTS.slippage))
    assert stop["time"].iloc[0] == T0 + 191 * HOUR


def test_intrabar_touch_fills_at_stop() -> None:
    o = np.full(200, 100.0)
    lo = o.copy()
    lo[191] = 94.0
    res = run_one(candles(o, o, lo), Script({180: (10, 95)}, set()))
    stop = res.fills[res.fills["reason"] == "stop"]
    assert stop["price"].iloc[0] == pytest.approx(95 * (1 - COSTS.slippage))


def test_close_stop_variant_ignores_lows() -> None:
    o = np.full(200, 100.0)
    lo = o.copy()
    lo[191] = 94.0
    res = run_one(candles(o, o, lo), Script({180: (10, 95)}, set()), intrabar=False)
    assert (res.fills["reason"] != "stop").all()


# --- real strategy on random walks -----------------------------------------------------------


def walk(n: int, seed: int, drift: float = 0.0003) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.008, n)))
    open_ = np.concatenate([[100.0], close[:-1]]) * np.exp(rng.normal(0, 0.001, n))
    spread = np.abs(rng.normal(0, 0.004, n)) * close
    df = candles(open_, close)
    df["high"] = np.maximum(open_, close) + spread
    df["low"] = np.minimum(open_, close) - spread
    return df


PAIRS = {"A/USDT": walk(4000, 1), "B/USDT": walk(4000, 2)}
FEATS = {s: snap.features(df, HOUR, FeatureParams.from_config(CONFIG)) for s, df in PAIRS.items()}
BASELINE = Baseline(BaselineParams.from_config(CONFIG))


def run_pairs(
    end: pd.Timestamp | None = None,
    intrabar: bool = True,
    max_pos: float = 10,
    max_total: float = 15,
) -> Result:
    cfg = EngineConfig(10_000.0, COSTS, intrabar, loose(max_pos, max_total))
    start = T0
    end = end if end is not None else T0 + 4000 * HOUR
    return run(PAIRS, FEATS, BASELINE, cfg, start, end, HOUR)


@pytest.mark.parametrize("intrabar", [True, False])
def test_fills_happen_at_open_of_candle_after_signal(intrabar: bool) -> None:
    res = run_pairs(intrabar=intrabar)
    signal_fills = res.fills[res.fills["reason"].isin(["entry", "signal"])]
    assert len(signal_fills) >= 4
    for f in signal_fills.itertuples():
        candle = (
            PAIRS[str(f.symbol)].set_index("timestamp").loc[f.time]
        )  # fill candle, keyed by open
        assert f.time == f.signal_time  # signal candle closes exactly when the fill candle opens
        assert f.raw_price == candle["open"]
        side = 1 if f.side == "buy" else -1
        assert f.price == pytest.approx(candle["open"] * (1 + side * COSTS.slippage))


def test_engine_causality() -> None:
    cut = T0 + 2500 * HOUR
    full = run_pairs()
    short = run_pairs(end=cut)
    # Everything strictly before the truncated run's final candle must be identical.
    pd.testing.assert_frame_equal(full.equity.loc[: cut - HOUR], short.equity.loc[: cut - HOUR])

    def early(f: pd.DataFrame) -> pd.DataFrame:
        return f[(f["time"] < cut) & (f["reason"] != "end")].reset_index(drop=True)

    pd.testing.assert_frame_equal(early(full.fills), early(short.fills))
    assert len(early(full.fills)) > 0


def test_accounting_and_caps() -> None:
    res = run_pairs()
    eq = res.equity
    values = eq[[c for c in eq.columns if c.startswith("value_")]].sum(axis=1)
    np.testing.assert_allclose(eq["equity"], eq["cash"] + values, rtol=1e-12)
    assert (eq["cash"] >= -1e-9).all()
    buys = res.fills[res.fills["side"] == "buy"]
    assert len(buys) > 0
    assert (buys["qty"] * buys["raw_price"] <= 0.10 * buys["equity_at_fill"] + 1e-9).all()
    assert (buys["exposure_after"] <= 0.15 * buys["equity_at_fill"] + 1e-9).all()
    # per-pair P&L curves sum back to total equity
    pnl = eq[[c for c in eq.columns if c.startswith("pnl_")]].sum(axis=1)
    np.testing.assert_allclose(eq["equity"], 10_000 + pnl, rtol=1e-12)


def test_engine_deterministic() -> None:
    a, b = run_pairs(), run_pairs()
    pd.testing.assert_frame_equal(a.equity, b.equity)
    pd.testing.assert_frame_equal(a.trades, b.trades)


# --- documented outages (market-closed gaps) ---------------------------------------------


def test_pending_orders_are_cancelled_across_a_gap() -> None:
    flat = [100.0] * 200
    df = candles(flat, flat).drop(index=[181, 182]).reset_index(drop=True)  # outage after 180
    res = run_one(df, Script({180: (10, 50)}, set()))
    assert res.fills.empty  # entry signalled at the last pre-gap close never fills


def test_stop_checked_at_first_open_after_gap() -> None:
    o = np.full(200, 100.0)
    lo = o.copy()
    o[183], lo[183] = 80.0, 79.0
    df = candles(o, o, lo).drop(index=[181, 182]).reset_index(drop=True)
    res = run_one(df, Script({170: (10, 95)}, set()))
    stop = res.fills[res.fills["reason"] == "stop"]
    assert stop["time"].iloc[0] == T0 + 183 * HOUR
    assert stop["price"].iloc[0] == pytest.approx(80 * (1 - COSTS.slippage))


def test_per_symbol_strategies() -> None:
    flat = [100.0] * 200
    df = candles(flat, flat)
    cfg = EngineConfig(10_000.0, COSTS, True, loose())
    pairs = {"A/USDT": df, "B/USDT": df}
    feats = {s: snap.features(df, HOUR, TOY_PARAMS) for s in pairs}
    strategies = {"A/USDT": Script({180: (1, 50)}, set()), "B/USDT": Script({}, set())}
    res = run(pairs, feats, strategies, cfg, T0, T0 + 200 * HOUR, HOUR)
    assert set(res.fills["symbol"]) == {"A/USDT"}
