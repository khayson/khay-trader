"""Strategy v1 (candidate A, EMA 20/100 on 4h) through the paper loop on ~90 days of real
BTC/ETH candles, including the documented 2023-03-24 outage: the loop must reproduce the
backtest engine trade for trade. Skipped until the 2018+ candles are backfilled."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
import yaml
from fake_exchange import FakeExchange, minutes_from_hours
from test_paper_loop import NO_DELAY, comparable

from trader.data import store
from trader.data.resample import resample
from trader.exec import divergence
from trader.exec.alerts import Alerter
from trader.exec.journal import Journal
from trader.exec.paper import PaperBroker
from trader.loop import PaperLoop, Settings
from trader.risk import killswitch as ks
from trader.state.snapshot import features

ROOT = Path(__file__).parent.parent
HOUR = pd.Timedelta(hours=1)
STEP = pd.Timedelta(hours=4)
DATA_START = pd.Timestamp("2022-11-01", tz="UTC")  # >= 500 4h bars of warmup before trading
FIRST_WAKE = pd.Timestamp("2023-02-01", tz="UTC")
LAST_WAKE = pd.Timestamp("2023-05-01", tz="UTC")
OUTAGE = pd.Timestamp("2023-03-24 13:00", tz="UTC")
PATHS = {s: store.candle_path(ROOT / "data", s, "1h") for s in ["BTC/USDT", "ETH/USDT"]}


def available() -> bool:
    return all(
        p.exists() and store.load(p)["timestamp"].iloc[0] <= DATA_START for p in PATHS.values()
    )


@pytest.mark.skipif(not available(), reason="needs 1h candles from before 2022-11")
def test_candidate_a_paper_replay_on_real_candles(tmp_path: Path) -> None:
    hours = {s: store.load(p) for s, p in PATHS.items()}
    hours = {
        s: h[(h["timestamp"] >= DATA_START) & (h["timestamp"] < LAST_WAKE + 2 * STEP)]
        for s, h in hours.items()
    }
    assert all(OUTAGE not in set(h["timestamp"]) for h in hours.values())  # really missing
    cfg = yaml.safe_load((ROOT / "config" / "default.yaml").read_text())
    settings = replace(
        Settings.from_config(cfg, ROOT),
        data_dir=tmp_path / "data",
        ks_path=tmp_path / "ks.json",
        initial_equity=10_000.0,
        history_hours=int((LAST_WAKE - DATA_START) / HOUR) + 24,
        retry_delays=NO_DELAY,
    )
    assert (settings.candidate.params.ema_fast, settings.candidate.params.ema_slow) == (20, 100)
    ks.save(settings.ks_path, ks.rearm(ks.load(settings.ks_path), "test", FIRST_WAKE, 1e4))
    fx = FakeExchange({s: minutes_from_hours(h) for s, h in hours.items()}, FIRST_WAKE)
    journal = Journal(tmp_path / "paper.db")
    loop = PaperLoop(
        settings,
        fx,
        journal,
        PaperBroker(fx, settings.costs, "modeled", retry_delays=NO_DELAY),
        Alerter(),
        clock=lambda: fx.now,
    )
    t = FIRST_WAKE
    while t <= LAST_WAKE:
        fx.now = t + pd.Timedelta(seconds=10)
        loop.step(fx.now)
        t += STEP

    trades = journal.frame("trades")
    assert len(trades) >= 10, "90 days of v1 should trade"
    assert set(trades["exit_reason"]) >= {"signal", "stop"}
    assert journal.outages("BTC/USDT") == []  # documented, so not a runtime outage
    assert not ks.load(settings.ks_path).tripped  # daily divergence checks were clean
    assert "divergence" not in set(journal.frame("events")["kind"])

    candles = {s: store.load(store.candle_path(settings.data_dir, s, "1h")) for s in PATHS}
    bars = {s: resample(c, "4h", HOUR, settings.outages[s]) for s, c in candles.items()}
    assert OUTAGE.floor(STEP) in set(bars["BTC/USDT"]["timestamp"])  # built from 3 hours
    feats = {s: features(b, STEP, settings.candidate.params) for s, b in bars.items()}
    result = divergence.replay(
        bars,
        feats,
        settings.candidate.strategy(settings.sizing),
        settings.costs,
        settings.limits,
        10_000.0,
        FIRST_WAKE,
        LAST_WAKE,
        STEP,
    )
    assert comparable(trades, LAST_WAKE) == comparable(result.trades, LAST_WAKE)
