from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd
import pytest
from fake_exchange import FakeExchange, make_minutes

from trader import loop as paper_loop
from trader.backtest.costs import Costs
from trader.backtest.engine import Result
from trader.backtest.research import Candidate
from trader.data import store
from trader.data.resample import resample
from trader.exec import divergence
from trader.exec.journal import Journal
from trader.exec.paper import PaperBroker, walk_book
from trader.loop import PaperLoop, Settings, single_instance
from trader.risk import killswitch as ks
from trader.risk.limits import RiskLimits
from trader.state.snapshot import FeatureParams, features
from trader.strategy.base import StopSizing

T0 = pd.Timestamp("2025-01-01", tz="UTC")
STEP = pd.Timedelta(hours=4)
HOURS = 700
SYMBOLS = ("AAA/USDT", "BBB/USDT")
CANDIDATE = Candidate(
    "T",
    "4h",
    "ema_trend",
    FeatureParams(
        ema_fast=3,
        ema_slow=6,
        atr_period=5,
        vol_window_hours=24,
        drawdown_window_hours=24,
        channel_entry=5,
        channel_exit=5,
    ),
)  # warmup 43 bars (7-day return)
COSTS = Costs(fee=0.001, slippage=0.0005, stop_slippage=0.0015)
FIRST_WAKE = T0 + 50 * STEP
LAST_WAKE = T0 + 170 * STEP
MINUTES = {s: make_minutes(T0, HOURS, seed) for seed, s in enumerate(SYMBOLS, start=1)}
ON_TIME = pd.Timedelta(seconds=5)
Row = tuple[str, pd.Timestamp, pd.Timestamp, str, float, float]


@dataclass
class World:
    fx: FakeExchange
    journal: Journal
    loop: PaperLoop
    settings: Settings

    def wake(self, t: pd.Timestamp, delay: pd.Timedelta = ON_TIME) -> None:
        self.fx.now = t + delay
        self.loop.step(self.fx.now)

    def run(self, start: pd.Timestamp, end: pd.Timestamp) -> None:
        t = start
        while t <= end:
            self.wake(t)
            t += STEP

    def restart(self, fill_model: str = "modeled") -> None:
        self.loop = PaperLoop(
            self.settings, self.fx, self.journal, PaperBroker(self.fx, COSTS, fill_model)
        )


def make_world(tmp_path: Path, flatten: bool = True, fill_model: str = "modeled") -> World:
    settings = Settings(
        symbols=SYMBOLS,
        candidate=CANDIDATE,
        sizing=StopSizing(3.0, 0.5, 10),
        limits=RiskLimits(10, 20, 2, 6, 180),
        ks_limits=ks.KillSwitchLimits(max_drawdown_pct=50, max_consecutive_api_errors=5),
        ks_path=tmp_path / "ks.json",
        costs=COSTS,
        initial_equity=10_000.0,
        flatten_on_trip=flatten,
        data_dir=tmp_path / "data",
        outages={},
        history_hours=HOURS,
        parity_bars=5,
        divergence_min_fills=10,
        divergence_slippage_mult=3.0,
    )
    ks.save(settings.ks_path, ks.rearm(ks.load(settings.ks_path), "test", T0, 10_000.0))
    fx = FakeExchange(MINUTES, FIRST_WAKE)
    journal = Journal(tmp_path / "paper.db")
    loop = PaperLoop(settings, fx, journal, PaperBroker(fx, COSTS, fill_model))
    return World(fx, journal, loop, settings)


def engine_replay(w: World, last_close: pd.Timestamp) -> Result:
    candles = {s: store.load(store.candle_path(w.settings.data_dir, s, "1h")) for s in SYMBOLS}
    bars = {s: resample(c, "4h", pd.Timedelta(hours=1)) for s, c in candles.items()}
    feats = {s: features(b, STEP, CANDIDATE.params) for s, b in bars.items()}
    return divergence.replay(
        bars,
        feats,
        CANDIDATE.strategy(w.settings.sizing),
        COSTS,
        w.settings.limits,
        10_000.0,
        FIRST_WAKE,
        last_close,
        STEP,
    )


def comparable(trades: pd.DataFrame, horizon: pd.Timestamp) -> list[Row]:
    t = trades[trades["exit_reason"].isin(["signal", "stop"])]
    out: list[Row] = []
    for sym, entry, exit_, reason, qty, pnl in zip(
        t["symbol"],
        t["entry_time"],
        t["exit_time"],
        t["exit_reason"],
        t["qty"],
        t["pnl"],
        strict=True,
    ):
        exit_bar = pd.Timestamp(exit_).floor(STEP)
        if exit_bar < horizon:
            out.append(
                (
                    str(sym),
                    pd.Timestamp(entry).floor(STEP),
                    exit_bar,
                    str(reason),
                    round(float(qty), 9),
                    round(float(pnl), 6),
                )
            )
    return sorted(out)


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> World:
    w = make_world(tmp_path_factory.mktemp("reference"))
    w.run(FIRST_WAKE, LAST_WAKE)
    return w


# --- replay parity ---------------------------------------------------------------------------


def test_paper_loop_reproduces_backtest_trade_for_trade(reference: World) -> None:
    w = reference
    trades = w.journal.frame("trades")
    assert len(trades) >= 6, "synthetic data should produce several trades"
    assert set(trades["exit_reason"]) >= {"signal", "stop"}
    result = engine_replay(w, LAST_WAKE)
    assert comparable(trades, LAST_WAKE) == comparable(result.trades, LAST_WAKE)
    mismatches = divergence.compare(
        trades, w.journal.positions(), result, LAST_WAKE, STEP, w.journal.frame("fills")
    )
    assert mismatches == []
    assert not ks.load(w.settings.ks_path).tripped  # daily divergence checks found nothing


def test_signal_fills_happen_at_the_new_bar_open(reference: World) -> None:
    fills = reference.journal.frame("fills")
    signal = fills[fills["reason"].isin(["entry", "signal"])]
    assert (signal["time"] == signal["signal_time"]).all()
    for sym, t, raw in zip(signal["symbol"], signal["time"], signal["raw_price"], strict=True):
        m = MINUTES[str(sym)]
        assert raw == m.loc[m["timestamp"] == t, "open"].iloc[0]


def test_journal_records_decisions_equity_and_heartbeat(reference: World) -> None:
    j = reference.journal
    decisions = j.frame("decisions")
    assert set(decisions["symbol"]) == set(SYMBOLS)
    assert decisions["bar_close"].max() == LAST_WAKE
    assert len(j.frame("equity", order="time")) == (LAST_WAKE - FIRST_WAKE) / STEP + 1
    assert j.get_time("heartbeat") == LAST_WAKE + pd.Timedelta(seconds=5)
    assert j.frame("parity").empty  # our 4h bars match the native klines


# --- crash and restart -----------------------------------------------------------------------


def test_restart_and_crash_mid_wake_reproduce_the_uninterrupted_run(
    tmp_path: Path, reference: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path)
    ref_trades = reference.journal.frame("trades")
    entry = ref_trades["entry_time"].iloc[2]
    w.run(FIRST_WAKE, entry - STEP)

    # Crash in the middle of the wake that should enter: nothing from it may persist.
    before = (w.journal.get("cash"), w.journal.get("last_bar_close"), w.journal.positions())

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("power cut")

    with monkeypatch.context() as patch:
        patch.setattr(w.loop.broker, "market", boom)
        with pytest.raises(RuntimeError):
            w.wake(entry)
    assert (w.journal.get("cash"), w.journal.get("last_bar_close"), w.journal.positions()) == before

    w.restart()  # new process: broker stops rebuilt from the journal
    w.wake(entry)
    assert w.journal.positions(), "the redone wake enters"
    held = next(iter(w.journal.positions().values()))
    w.restart()
    assert w.loop.broker.resting_stops()[held.symbol] == (held.qty, held.stop_price)
    w.run(entry + STEP, LAST_WAKE)
    assert comparable(w.journal.frame("trades"), LAST_WAKE) == comparable(ref_trades, LAST_WAKE)


# --- late wake and stale data ----------------------------------------------------------------


def _first(reference: World, column: str, reason: str | None = None) -> pd.Timestamp:
    trades = reference.journal.frame("trades")
    if reason:
        trades = trades[trades["exit_reason"] == reason]
    return pd.Timestamp(trades[column].iloc[0]).floor(STEP)


def test_missed_bar_skips_entry(tmp_path: Path, reference: World) -> None:
    w = make_world(tmp_path)
    entry = _first(reference, "entry_time")
    w.run(FIRST_WAKE, entry - STEP)
    w.wake(entry, delay=pd.Timedelta(hours=1))  # an hour late
    decisions = w.journal.frame("decisions")
    late = decisions[(decisions["bar_close"] == entry) & (decisions["action"] == "enter")]
    assert len(late) >= 1
    assert late["risk"].str.startswith("rejected: stale data").all()
    assert w.journal.positions() == {}


def test_missed_bar_still_exits(tmp_path: Path, reference: World) -> None:
    w = make_world(tmp_path)
    exit_bar = _first(reference, "exit_time", reason="signal")
    w.run(FIRST_WAKE, exit_bar - STEP)
    assert w.journal.positions()
    w.wake(exit_bar, delay=pd.Timedelta(hours=1))
    trades = w.journal.frame("trades")
    exits = trades[trades["exit_reason"] == "signal"]
    assert exits["exit_time"].max() == exit_bar + pd.Timedelta(hours=1)  # at the late price


def test_stale_feed_blocks_entry_allows_exit(tmp_path: Path, reference: World) -> None:
    w = make_world(tmp_path)
    exit_bar = _first(reference, "exit_time", reason="signal")
    w.run(FIRST_WAKE, exit_bar - STEP)
    # The wake at exit_bar is missed and the candle feed lags an hour behind the next one:
    # the newest complete bar is the one with the exit signal, but it is stale.
    w.fx.feed_cutoff = exit_bar + STEP - pd.Timedelta(hours=1)
    w.wake(exit_bar + STEP)
    decisions = w.journal.frame("decisions")
    assert decisions["bar_close"].max() == exit_bar
    assert (w.journal.frame("events")["kind"] == "stale_feed").any()  # priced from the book
    trades = w.journal.frame("trades")
    assert (trades["exit_time"] == exit_bar + STEP).any()  # exit executed anyway
    wake = decisions[decisions["wake_time"] == exit_bar + STEP + pd.Timedelta(seconds=5)]
    entries = wake[wake["action"] == "enter"]
    assert entries["risk"].str.startswith("rejected: stale data").all()


# --- kill switch -----------------------------------------------------------------------------


def _hold_position(tmp_path: Path, reference: World, flatten: bool) -> tuple[World, pd.Timestamp]:
    w = make_world(tmp_path, flatten=flatten)
    entry = _first(reference, "entry_time")
    w.run(FIRST_WAKE, entry)
    assert w.journal.positions()
    return w, entry


def test_trip_flattens_then_cancels_stops_and_blocks_entries(
    tmp_path: Path, reference: World
) -> None:
    w, entry = _hold_position(tmp_path, reference, flatten=True)
    ks.save(w.settings.ks_path, ks.trip(ks.load(w.settings.ks_path), "manual: drill", entry))
    w.run(entry + STEP, LAST_WAKE)
    fills = w.journal.frame("fills")
    after = fills[fills["time"] > entry]
    assert (after.loc[after["side"] == "buy"]).empty
    assert "killswitch" in set(after["reason"])
    assert w.journal.positions() == {}
    assert w.loop.broker.resting_stops() == {}


def test_trip_without_flatten_keeps_position_and_stop(tmp_path: Path, reference: World) -> None:
    w, entry = _hold_position(tmp_path, reference, flatten=False)
    ks.save(w.settings.ks_path, ks.trip(ks.load(w.settings.ks_path), "manual: drill", entry))
    held = dict(w.journal.positions())
    w.wake(entry + STEP)
    for s in held:
        if s in w.journal.positions():  # unless its own stop or exit signal closed it
            assert s in w.loop.broker.resting_stops()
    fills = w.journal.frame("fills")
    assert fills.loc[fills["time"] > entry, "side"].isin(["sell"]).all()


def test_api_error_burst_trips(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.run(FIRST_WAKE, FIRST_WAKE + STEP)
    w.fx.fail_next = 10**6
    for k in range(2, 7):
        w.wake(FIRST_WAKE + k * STEP)
    state = ks.load(w.settings.ks_path)
    assert state.tripped and "5 consecutive API errors" in state.reason
    audit = ks.audit_path(w.settings.ks_path).read_text()
    assert "API errors" in audit
    assert (w.journal.frame("events")["kind"] == "api_error").sum() == 5


def test_corrupt_state_file_blocks_all_entries(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.settings.ks_path.write_text("{corrupt")
    w.run(FIRST_WAKE, LAST_WAKE)
    fills = w.journal.frame("fills")
    assert (fills["side"] == "buy").sum() == 0
    decisions = w.journal.frame("decisions")
    entries = decisions[decisions["action"] == "enter"]
    assert len(entries) > 0
    assert (entries["risk"] == "rejected: kill switch tripped").all()


# --- data parity and broker ------------------------------------------------------------------


def test_native_kline_mismatch_is_logged(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    bar = FIRST_WAKE - 2 * STEP
    w.fx.native_overrides[("AAA/USDT", bar)] = 1.0
    w.wake(FIRST_WAKE)
    parity = w.journal.frame("parity")
    assert len(parity) == 1
    row = parity.iloc[0]
    assert (row["symbol"], row["bar_open"], row["field"], row["native"]) == (
        "AAA/USDT",
        bar,
        "close",
        1.0,
    )


def test_book_fill_records_model_alongside(tmp_path: Path) -> None:
    w = make_world(tmp_path, fill_model="book")
    w.run(FIRST_WAKE, LAST_WAKE)
    fills = w.journal.frame("fills")
    buys = fills[fills["reason"] == "entry"]
    assert len(buys) > 0
    assert (buys["price"] > buys["raw_price"]).all()
    assert (buys["modeled_price"] == buys["raw_price"] * (1 + COSTS.slippage)).all()
    n, avg, warn = divergence.slippage_check(fills, COSTS.slippage, 10, 3.0)
    assert n > 0 and avg > 0 and not warn


def test_walk_book() -> None:
    levels = [[100.0, 1.0], [101.0, 1.0], [102.0, 5.0]]
    assert walk_book(levels, 0.5) == (100.0, True)
    assert walk_book(levels, 2.0) == (100.5, True)
    price, covered = walk_book(levels, 10.0)  # 7 visible: rest at the worst level
    assert not covered and price == pytest.approx((100 + 101 + 5 * 102 + 3 * 102) / 10)
    with pytest.raises(ValueError):
        walk_book([], 1.0)


def test_stop_gap_through_fills_at_minute_open() -> None:
    m = MINUTES["AAA/USDT"].copy()
    i = 1000
    level = float(m["open"].iloc[i]) * 1.01  # stop above the open: gapped through
    fx = FakeExchange({"AAA/USDT": m}, m["timestamp"].iloc[i + 5])
    broker = PaperBroker(fx, COSTS, "modeled")
    broker.set_stop("AAA/USDT", 2.0, level)
    since = m["timestamp"].iloc[i]
    fills = broker.poll_stop_fills(since, since + pd.Timedelta(minutes=5))
    assert len(fills) == 1 and fills[0].time == since
    assert fills[0].price == pytest.approx(m["open"].iloc[i] * (1 - 0.0015))
    assert broker.resting_stops() == {}


def test_single_instance_lock(tmp_path: Path) -> None:
    lock = tmp_path / "trader.lock"
    with single_instance(lock):
        with pytest.raises(RuntimeError):
            with single_instance(lock):
                pass
    with single_instance(lock):  # released
        pass


def test_live_broker_is_a_stub() -> None:
    from trader.exec.live import LiveBroker

    with pytest.raises(NotImplementedError):
        LiveBroker()


def test_settings_from_default_config() -> None:
    import yaml

    root = Path(__file__).parent.parent
    cfg = yaml.safe_load((root / "config" / "default.yaml").read_text())
    s = Settings.from_config(cfg, root)
    assert s.candidate.name == "A" and s.candidate.timeframe == "4h"
    assert (s.candidate.params.ema_fast, s.candidate.params.ema_slow) == (20, 100)
    assert s.costs.stop_slippage == 0.0015 and s.flatten_on_trip
    assert s.ks_limits.max_drawdown_pct == 10
    assert replace(s.limits) == RiskLimits.from_config(cfg)
    assert paper_loop.HOUR == pd.Timedelta(hours=1)


# --- divergence monitor ----------------------------------------------------------------------


def test_divergence_mismatch_trips_kill_switch(tmp_path: Path, reference: World) -> None:
    w = make_world(tmp_path)
    exit_bar = _first(reference, "exit_time")
    w.run(FIRST_WAKE, exit_bar + STEP)
    assert not ks.load(w.settings.ks_path).tripped
    with w.journal.transaction():  # paper "forgets" a trade the backtest made
        w.journal.conn.execute("DELETE FROM trades WHERE id = (SELECT MIN(id) FROM trades)")
        w.journal.set("divergence_day", T0)  # force today's check to run again
    w.wake(exit_bar + 2 * STEP)
    state = ks.load(w.settings.ks_path)
    assert state.tripped and "divergence" in state.reason
    assert (w.journal.frame("events")["kind"] == "divergence").any()
    assert "divergence" in ks.audit_path(w.settings.ks_path).read_text()


def test_slippage_warning_threshold() -> None:
    def fills(n: int, slip: float) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "reason": ["entry"] * n,
                "side": ["buy"] * n,
                "raw_price": [100.0] * n,
                "price": [100.0 * (1 + slip)] * n,
            }
        )

    assert divergence.slippage_check(fills(10, 0.0016), 0.0005, 10, 3.0)[2]  # > 3x over 10
    assert not divergence.slippage_check(fills(9, 0.0016), 0.0005, 10, 3.0)[2]  # too few
    assert not divergence.slippage_check(fills(10, 0.0014), 0.0005, 10, 3.0)[2]  # under 3x
