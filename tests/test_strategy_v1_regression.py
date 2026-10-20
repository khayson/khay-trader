"""Strategy v1 (candidate A) must reproduce the research round 1 trades exactly.

Skipped until the 2018+ candles are backfilled. A failure means the engine, risk layer or
features changed behaviour: explain it before touching the expected CSV.
"""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from trader.backtest import research
from trader.backtest.costs import Costs
from trader.backtest.engine import EngineConfig, run
from trader.backtest.report import INITIAL_EQUITY
from trader.data import store
from trader.data.resample import resample
from trader.risk.limits import RiskLimits
from trader.state.snapshot import features
from trader.strategy.base import StopSizing

ROOT = Path(__file__).parent.parent
EXPECTED = ROOT / "reports" / "research_round1_2026-10-19_trades_A.csv"
PATHS = {s: store.candle_path(ROOT / "data", s, "1h") for s in ["BTC/USDT", "ETH/USDT"]}
HOUR = pd.Timedelta(hours=1)
ROUND1_DATA_END = pd.Timestamp("2026-10-19 22:00", tz="UTC")  # see the round-1 report


def data_available() -> bool:
    if not EXPECTED.exists() or not all(p.exists() for p in PATHS.values()):
        return False
    return all(
        store.load(p)["timestamp"].iloc[0] <= pd.Timestamp("2018-01-01", tz="UTC")
        for p in PATHS.values()
    )


@pytest.mark.skipif(not data_available(), reason="needs 2018+ candles and the round-1 CSV")
def test_candidate_a_trades_unchanged() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "default.yaml").read_text())
    protocol = yaml.safe_load((ROOT / "config" / "research_round1.yaml").read_text())
    cand = research.Candidate.from_protocol("A", protocol["candidates"]["A"], protocol)
    step = pd.Timedelta(cand.timeframe)
    gaps = ROOT / "config" / "known_gaps.yaml"
    data = {
        s: resample(store.load(p), cand.timeframe, HOUR, store.load_known_gaps(gaps, s, "1h"))
        for s, p in PATHS.items()
    }
    feats = {s: features(df, step, cand.params) for s, df in data.items()}
    risk = cfg["risk"]
    strategy = cand.strategy(
        StopSizing(protocol["stop_atr_mult"], risk["risk_per_trade_pct"], risk["max_position_pct"])
    )
    engine_cfg = EngineConfig(
        INITIAL_EQUITY, Costs.from_config(cfg), True, RiskLimits.from_config(cfg)
    )
    expected = pd.read_csv(EXPECTED)
    # Round 1's open-ended test split ended where its data ended; pin it so newer candles
    # (the paper loop keeps the store fresh) do not add trades.
    data_end = ROUND1_DATA_END
    for name, (start, end) in protocol["splits"].items():
        end_ts = data_end if end is None else pd.Timestamp(end, tz="UTC")
        res = run(data, feats, strategy, engine_cfg, pd.Timestamp(start, tz="UTC"), end_ts, step)
        # The cap clips almost every entry (expected); nothing may be dropped or rejected.
        assert set(res.rejections["kind"]) <= {"clipped"}, res.rejections
        got = res.trades
        want = expected[expected["split"] == name].reset_index(drop=True)
        assert len(got) == len(want), name
        for col in ["entry_time", "exit_time"]:
            assert list(got[col].astype(str)) == list(want[col]), (name, col)
        for col in ["qty", "entry_price", "exit_price", "pnl", "fees", "slippage"]:
            pd.testing.assert_series_equal(
                got[col].astype(float), want[col].astype(float), check_names=False, rtol=1e-12
            )
