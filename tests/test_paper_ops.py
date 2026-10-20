"""Operational behaviour of the paper loop: failures that must not skip exits, runtime
outages, divergence classification, exchange rules, clock, prompt flatten, crashes, alerts."""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from fake_exchange import FakeExchange
from test_paper_loop import (
    FIRST_WAKE,
    LAST_WAKE,
    MINUTES,
    STEP,
    SYMBOLS,
    World,
    _first,
    comparable,
    engine_replay,
    make_world,
)

from trader.data import store
from trader.data.resample import resample
from trader.exec import divergence
from trader.exec.alerts import Alerter
from trader.exec.journal import PositionRow
from trader.loop import EquityMismatch, PaperLoop, git_state
from trader.risk import killswitch as ks
from trader.state.snapshot import features

HOUR = pd.Timedelta(hours=1)


def kinds(w: World) -> set[str]:
    return set(w.journal.frame("events")["kind"])


def force_divergence_check(w: World) -> None:
    with w.journal.transaction():
        w.journal.set("divergence_day", pd.Timestamp("2000-01-01", tz="UTC"))


# --- A1: a failing diagnostic never skips an exit ----------------------------------------


def test_parity_api_failure_still_executes_exit(tmp_path: Path, reference: World) -> None:
    w = make_world(tmp_path)
    exit_bar = _first(reference, "exit_time", reason="signal")
    w.run(FIRST_WAKE, exit_bar - STEP)
    w.fx.fail_timeframes = {"4h"}  # native klines endpoint down for good
    w.wake(exit_bar)
    trades = w.journal.frame("trades")
    assert (trades["exit_time"] == exit_bar).any()
    assert "parity_error" in kinds(w)
    assert not ks.load(w.settings.ks_path).tripped


# --- A2: unknown outages -----------------------------------------------------------------


def test_unknown_outage_is_detected_persisted_and_survived(tmp_path: Path) -> None:
    hole = FIRST_WAKE + 20 * STEP + HOUR  # an hour inside the bar opening at FIRST_WAKE+20*STEP
    minutes = {
        s: m[(m["timestamp"] < hole) | (m["timestamp"] >= hole + HOUR)] for s, m in MINUTES.items()
    }
    w = make_world(tmp_path, minutes=minutes)
    w.run(FIRST_WAKE, LAST_WAKE)  # must not crash
    assert {s for s in SYMBOLS if w.journal.outages(s) == [(hole, hole)]} == set(SYMBOLS)
    assert "outage" in kinds(w)
    assert any(title == "exchange outage detected" for title, _ in w.loop.alerter.sent)
    slots = w.journal.wake_slots().set_index("slot")
    assert slots.at[FIRST_WAKE + 21 * STEP, "status"] == "outage"
    decisions = w.journal.frame("decisions")
    in_bar = decisions[
        (decisions["bar_close"] == FIRST_WAKE + 21 * STEP) & (decisions["action"] == "enter")
    ]
    assert in_bar["risk"].str.startswith("rejected: outage").all()
    assert not ks.load(w.settings.ks_path).tripped
    # The bar was built from the hours that traded, like a documented outage.
    candles = store.load(store.candle_path(w.settings.data_dir, SYMBOLS[0], "1h"))
    gap = store.Gap(hole, hole, 1)
    bars = resample(candles, "4h", HOUR, [gap])
    assert FIRST_WAKE + 20 * STEP in set(bars["timestamp"])


def test_late_candle_resolves_runtime_outage(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    hole = FIRST_WAKE + 3 * STEP + HOUR
    w.fx.hidden_hours = {hole}
    w.run(FIRST_WAKE, FIRST_WAKE + 5 * STEP)
    assert w.journal.outages(SYMBOLS[0]) == [(hole, hole)]
    w.fx.hidden_hours = set()  # the exchange publishes it late
    w.run(FIRST_WAKE + 6 * STEP, FIRST_WAKE + 7 * STEP)
    assert w.journal.outages(SYMBOLS[0]) == []
    assert "outage_resolved" in kinds(w)
    candles = store.load(store.candle_path(w.settings.data_dir, SYMBOLS[0], "1h"))
    assert hole in set(candles["timestamp"])
    bars = resample(candles, "4h", HOUR, [store.Gap(hole, hole, 1)])  # stale outage list
    bar = bars.set_index("timestamp").loc[FIRST_WAKE + 3 * STEP]
    m = MINUTES[SYMBOLS[0]]
    in_bar = m[(m["timestamp"] >= FIRST_WAKE + 3 * STEP) & (m["timestamp"] < FIRST_WAKE + 4 * STEP)]
    assert bar["volume"] == pytest.approx(in_bar["volume"].sum())  # all four hours


def test_initial_equity_is_pinned_in_the_journal(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.run(FIRST_WAKE, FIRST_WAKE + STEP)
    assert w.journal.get_float("initial_equity") == 10_000.0
    changed = replace(w.settings, initial_equity=25_000.0)
    with pytest.raises(EquityMismatch, match="10000.00"):
        PaperLoop(changed, w.fx, w.journal, w.loop.broker)
    PaperLoop(w.settings, w.fx, w.journal, w.loop.broker)  # same value: fine


# --- B4: divergence classification -------------------------------------------------------


def test_missed_wake_is_recorded_and_explained(tmp_path: Path, reference: World) -> None:
    w = make_world(tmp_path)
    entry = _first(reference, "entry_time")
    w.run(FIRST_WAKE, entry - STEP)
    w.run(entry + STEP, entry + 4 * STEP)  # the wake at `entry` never happened
    slots = w.journal.wake_slots().set_index("slot")
    assert slots.at[entry, "status"] == "missed"
    force_divergence_check(w)
    w.wake(entry + 5 * STEP)
    assert not ks.load(w.settings.ks_path).tripped
    assert "divergence_explained" in kinds(w)
    assert "divergence" not in kinds(w)


def test_too_many_missed_wakes_block_entries_without_flattening(
    tmp_path: Path, reference: World
) -> None:
    w = make_world(tmp_path)
    w.run(FIRST_WAKE, FIRST_WAKE + 2 * STEP)
    w.run(FIRST_WAKE + 6 * STEP, LAST_WAKE)  # three slots missed
    decisions = w.journal.frame("decisions")
    entries = decisions[decisions["action"] == "enter"]
    window_end = FIRST_WAKE + 3 * STEP + pd.Timedelta(days=7)  # the misses age out after
    blocked = entries[entries["bar_close"] < window_end]
    assert len(blocked) > 0
    assert blocked["risk"].str.startswith("rejected: missed wakes").all()
    assert (
        not entries[entries["bar_close"] > window_end + STEP]["risk"]
        .str.startswith("rejected: missed wakes")
        .any()
    )
    assert any(title == "missed wakes" for title, _ in w.loop.alerter.sent)
    assert not ks.load(w.settings.ks_path).tripped


def test_over_excusing_divergence_monitor_alerts(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.run(FIRST_WAKE, FIRST_WAKE + 2 * STEP)
    w.run(FIRST_WAKE + 6 * STEP, FIRST_WAKE + 20 * STEP)  # 3 of ~20 slots missed
    force_divergence_check(w)
    w.wake(FIRST_WAKE + 21 * STEP)
    assert "divergence_overexcused" in kinds(w)
    assert any(t == "divergence monitor over-excusing" for t, _ in w.loop.alerter.sent)


def test_revised_bars_detects_changed_features() -> None:
    t1, t2 = pd.Timestamp("2025-01-01 04:00", tz="UTC"), pd.Timestamp("2025-01-01 08:00", tz="UTC")
    feats = {"X": pd.DataFrame({"close": [1.0, 2.0], "atr": [0.1, 0.2]}, index=[t1, t2])}
    decisions = pd.DataFrame(
        {
            "symbol": ["X", "X", "X"],
            "bar_close": [t1, t2, t2],
            "features": [
                json.dumps({"close": 1.0, "atr": 0.1}),
                json.dumps({"close": 2.5}),
                "null",
            ],
        }
    )
    assert divergence.revised_bars(decisions, feats) == {t2}


def test_data_revision_is_explained_not_tripped(tmp_path: Path, reference: World) -> None:
    w = make_world(tmp_path)
    entry = _first(reference, "entry_time")
    w.run(FIRST_WAKE, entry + STEP)
    # The exchange revises a candle inside the decision bar after we traded on it.
    path = store.candle_path(w.settings.data_dir, SYMBOLS[0], "1h")
    candles = store.load(path)
    candles.loc[candles["timestamp"] == entry - HOUR, ["high", "close"]] *= 1.3
    store.save(candles, path, HOUR)
    bars = {
        s: resample(store.load(store.candle_path(w.settings.data_dir, s, "1h")), "4h", HOUR)
        for s in SYMBOLS
    }
    feats = {s: features(b, STEP, w.settings.candidate.params) for s, b in bars.items()}
    assert entry in divergence.revised_bars(w.journal.frame("decisions"), feats)
    force_divergence_check(w)
    w.wake(entry + 2 * STEP)
    assert "divergence" not in kinds(w)
    assert not ks.load(w.settings.ks_path).tripped


def test_divergence_trip_flattens_immediately(tmp_path: Path, reference: World) -> None:
    w = make_world(tmp_path)
    ref = reference.journal.frame("trades")
    second_entry = pd.Timestamp(ref["entry_time"].iloc[1]).floor(STEP)
    w.run(FIRST_WAKE, second_entry)
    assert w.journal.positions()
    with w.journal.transaction():  # paper "forgets" a trade the backtest made
        w.journal.conn.execute("DELETE FROM trades WHERE id = (SELECT MIN(id) FROM trades)")
    force_divergence_check(w)
    w.wake(second_entry + STEP)
    state = ks.load(w.settings.ks_path)
    assert state.tripped and "divergence" in state.reason
    assert w.journal.positions() == {}
    fills = w.journal.frame("fills")
    assert (fills["reason"] == "killswitch").any()
    assert w.loop.broker.resting_stops() == {}


# --- C6/C7: exchange rules and clock ------------------------------------------------------


def test_sub_minimum_entries_are_explained_rejections(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.fx.min_cost = 1e9
    w.run(FIRST_WAKE, LAST_WAKE)
    assert (w.journal.frame("fills")["side"] == "buy").sum() == 0
    entries = w.journal.frame("decisions").query("action == 'enter'")
    assert len(entries) > 0
    assert entries["risk"].str.startswith("rejected: exchange minimum").all()
    assert not ks.load(w.settings.ks_path).tripped  # explained, not divergence


def test_conform_truncates_to_lot_size(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.fx.amount_step = 0.001
    qty, why = w.loop.broker.conform("AAA/USDT", 1.23456, 100.0)
    assert why is None and qty == pytest.approx(1.234)
    assert w.loop.broker.conform("AAA/USDT", 0.0004, 100.0)[1] is not None


def test_clock_skew_blocks_entries_and_alerts(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.fx.clock_skew = pd.Timedelta(seconds=5)
    w.run(FIRST_WAKE, LAST_WAKE)
    entries = w.journal.frame("decisions").query("action == 'enter'")
    assert len(entries) > 0
    assert entries["risk"].str.startswith("rejected: clock skew").all()
    assert any(title == "clock skew" for title, _ in w.loop.alerter.sent)


# --- A3: prompt flatten and minute ticks -------------------------------------------------


def test_manual_trip_between_wakes_flattens_within_a_minute(
    tmp_path: Path, reference: World
) -> None:
    w = make_world(tmp_path)
    entry = _first(reference, "entry_time")
    w.run(FIRST_WAKE, entry)
    assert w.journal.positions()
    ks.save(w.settings.ks_path, ks.trip(ks.load(w.settings.ks_path), "manual: drill", entry))
    w.fx.now = entry + pd.Timedelta(minutes=17, seconds=30)
    w.loop.run_once(w.fx.now)  # not a wake slot: a tick
    assert w.journal.positions() == {}
    flat = w.journal.frame("fills").query("reason == 'killswitch'")
    assert list(flat["time"]) == [w.fx.now] * len(flat)
    assert w.loop.broker.resting_stops() == {}


def test_minute_ticks_keep_replay_parity(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    t = FIRST_WAKE
    while t <= LAST_WAKE:
        w.wake(t)
        for k in range(1, 8):  # every 30 minutes until the next bar
            w.fx.now = t + k * pd.Timedelta(minutes=30)
            w.loop.tick(w.fx.now)
        t += STEP
    trades = w.journal.frame("trades")
    assert (trades["exit_reason"] == "stop").any()
    assert comparable(trades, LAST_WAKE) == comparable(
        engine_replay(w, LAST_WAKE).trades, LAST_WAKE
    )


# --- C9: crashes, over-cap warnings, start-up --------------------------------------------


def test_consecutive_crashes_have_their_own_counter_and_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path)

    def crash(now: pd.Timestamp) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(w.loop, "_wake", crash)
    now = FIRST_WAKE + pd.Timedelta(seconds=10)
    for k in range(4):
        w.loop.run_once(now + k * pd.Timedelta(minutes=1))
    assert not ks.load(w.settings.ks_path).tripped
    w.loop.run_once(now + 4 * pd.Timedelta(minutes=1))
    state = ks.load(w.settings.ks_path)
    assert state.tripped and state.reason == "5 consecutive crashes"
    assert state.consecutive_api_errors == 0


def test_overcap_warning_once_per_crossing_then_daily(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = make_world(tmp_path)
    t = FIRST_WAKE
    with w.journal.transaction():
        w.journal.set("cash", 9_000.0)
        w.journal.upsert_position(PositionRow("AAA/USDT", 12.0, 100.0, t, 1200.0, 50.0))
        refs = {"AAA/USDT": 100.0, "BBB/USDT": 100.0}
        caplog.set_level(logging.WARNING, logger="trader.loop")
        for k in range(3):  # same day: one warning
            w.loop._overcap(t + k * STEP, refs)
        w.loop._overcap(t + pd.Timedelta(days=1), refs)  # next day: one more
    assert sum("over the 10% cap" in r.getMessage() for r in caplog.records) == 2


def test_git_state_detects_dirty_tree(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("a\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "c")
    state = git_state(tmp_path)
    assert state is not None and not state[1]
    (tmp_path / "f.txt").write_text("b\n")
    state = git_state(tmp_path)
    assert state is not None and state[1]
    assert git_state(tmp_path / "missing") is None


def test_alerts_never_log_urls(caplog: pytest.LogCaptureFixture) -> None:
    sent: list[urllib.request.Request] = []
    hook_url = "https://hooks.example.invalid/hook-1234"
    alerter = Alerter(webhook=hook_url, deadman=hook_url + "/ping", sender=sent.append)
    caplog.set_level(logging.DEBUG)
    alerter.alert("kill switch tripped", "drawdown 10%")
    assert sent == []  # queued, not delivered while a transaction may be open
    alerter.flush()
    alerter.ping()
    assert len(sent) == 2
    body = json.loads(sent[0].data)  # type: ignore[arg-type]
    assert "drawdown 10%" in body["text"] and body["text"] == body["content"]

    def broken(_: urllib.request.Request) -> None:
        raise OSError(hook_url)

    failing = Alerter(webhook=hook_url, sender=broken)
    failing.alert("x", "y")
    failing.flush()  # delivery error swallowed, URL not logged
    assert "hook-1234" not in caplog.text


def test_dead_man_pinged_on_every_tick(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    sent: list[urllib.request.Request] = []
    w.loop.alerter = Alerter(deadman="https://ping.example.invalid/x", sender=sent.append)
    w.wake(FIRST_WAKE)
    for k in range(1, 4):
        w.loop.tick(FIRST_WAKE + k * pd.Timedelta(minutes=1))
    assert len(sent) == 4  # one per wake and one per tick


def test_alert_history_is_bounded() -> None:
    alerter = Alerter()
    for k in range(1000):
        alerter.alert("t", str(k))
    alerter.flush()
    assert alerter.pending == [] and len(alerter.sent) == 200
    assert alerter.sent[-1] == ("t", "999")


def test_fake_exchange_is_deterministic() -> None:
    fx = FakeExchange(MINUTES, FIRST_WAKE)
    assert fx.fetch_ohlcv("AAA/USDT", "1h", limit=3) == fx.fetch_ohlcv("AAA/USDT", "1h", limit=3)
