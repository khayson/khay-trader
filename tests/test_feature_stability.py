"""The divergence monitor's "data revision" excuse must not fire spuriously. Features are
computed from a pinned start, so how much older history the store holds cannot change
them; a real change to a past candle flags only its own decision bar and later ones."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from trader.backtest.research import Candidate
from trader.data import store
from trader.exec.divergence import revised_bars
from trader.loop import feature_frame
from trader.state.snapshot import FEATURES

ROOT = Path(__file__).parent.parent
PATH = store.candle_path(ROOT / "data", "BTC/USDT", "1h")
START = pd.Timestamp("2022-06-01", tz="UTC")  # pinned feature start
END = pd.Timestamp("2023-03-01", tz="UTC")
HOUR = pd.Timedelta(hours=1)


def candidate_a() -> Candidate:
    protocol = yaml.safe_load((ROOT / "config" / "research_round1.yaml").read_text())
    return Candidate.from_protocol("A", protocol["candidates"]["A"], protocol)


def available() -> bool:
    return PATH.exists() and store.load(PATH)["timestamp"].iloc[0] <= START - pd.Timedelta(days=2)


def decisions_from(feats: pd.DataFrame, n: int = 300) -> pd.DataFrame:
    """Decision rows as the loop journals them, for the first n ready bars: just after the
    500-bar warmup, where the EMA seed still weighs most (the hardest case)."""
    ready = feats[feats["ready"]].head(n)
    return pd.DataFrame(
        {
            "symbol": "BTC/USDT",
            "bar_close": ready.index,
            "features": [
                json.dumps({f: float(row[f]) for f in FEATURES}) for _, row in ready.iterrows()
            ],
        }
    )


pytestmark = pytest.mark.skipif(not available(), reason="needs BTC 1h candles from mid-2022")


@pytest.fixture(scope="module")
def candles() -> pd.DataFrame:
    df = store.load(PATH)
    return df[df["timestamp"] < END].reset_index(drop=True)


@pytest.mark.parametrize("earlier", [HOUR, 4 * HOUR, 24 * HOUR])
def test_more_history_in_the_store_changes_nothing(
    candles: pd.DataFrame, earlier: pd.Timedelta
) -> None:
    a = candidate_a()
    _, base = feature_frame(candles[candles["timestamp"] >= START], START, a, [])
    _, wider = feature_frame(candles[candles["timestamp"] >= START - earlier], START, a, [])
    assert revised_bars(decisions_from(base), {"BTC/USDT": wider}) == set()


def test_unpinned_start_would_flag_every_bar(candles: pd.DataFrame) -> None:
    """Why the start is pinned: 4h more history shifts the slow EMA by more than 1e-9."""
    a = candidate_a()
    _, base = feature_frame(candles, START, a, [])
    _, shifted = feature_frame(candles, START - 4 * HOUR, a, [])
    decisions = decisions_from(base)
    assert len(revised_bars(decisions, {"BTC/USDT": shifted})) > len(decisions) // 2


def test_changed_past_candle_flags_its_bar_and_later_only(candles: pd.DataFrame) -> None:
    a = candidate_a()
    window = candles[candles["timestamp"] >= START].reset_index(drop=True)
    _, base = feature_frame(window, START, a, [])
    decisions = decisions_from(base)
    target = pd.Timestamp(decisions["bar_close"].iloc[100])  # a decision bar's close
    revised = window.copy()
    revised.loc[revised["timestamp"] == target - HOUR, ["high", "close"]] *= 1.01
    _, after = feature_frame(revised, START, a, [])
    flagged = revised_bars(decisions, {"BTC/USDT": after})
    assert min(flagged) == target
    assert all(t >= target for t in flagged)
    later = set(decisions["bar_close"].iloc[100:110])
    assert later <= flagged  # the recursive EMA carries the change forward
