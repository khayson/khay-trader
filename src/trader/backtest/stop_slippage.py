"""Sensitivity check: strategy v1 (candidate A) with stop fills slipping 1x, 3x and 6x the
modeled 0.05%. Entries and signal exits keep the normal slippage. Not a new test of the
strategy: the splits and parameters are those of research round 1, unchanged.

Run: python -m trader.backtest.stop_slippage
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import pandas as pd
import yaml

from trader.backtest.costs import Costs
from trader.backtest.engine import EngineConfig, run
from trader.backtest.report import (
    INITIAL_EQUITY,
    Split,
    _git_commit,
    _num,
    _pct,
    _scope_metrics,
    _sha256,
)
from trader.backtest.research import Candidate
from trader.data import store
from trader.data.resample import resample
from trader.risk.limits import RiskLimits
from trader.state.snapshot import features
from trader.strategy.base import StopSizing

MULTIPLIERS = (1, 3, 6)
HOUR = pd.Timedelta(hours=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--protocol", type=Path, default=Path("config/research_round1.yaml"))
    parser.add_argument("--known-gaps", type=Path, default=Path("config/known_gaps.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("reports/stop_slippage_sensitivity.md"))
    args = parser.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text())
    protocol = yaml.safe_load(args.protocol.read_text())
    risk = cfg["risk"]
    cand = Candidate.from_protocol("A", protocol["candidates"]["A"], protocol)
    step = pd.Timedelta(cand.timeframe)
    paths = {s: store.candle_path(args.data_dir, s, "1h") for s in cfg["pairs"]}
    candles = {s: store.load(p) for s, p in paths.items()}
    data = {
        s: resample(df, cand.timeframe, HOUR, store.load_known_gaps(args.known_gaps, s, "1h"))
        for s, df in candles.items()
    }
    feats = {s: features(df, step, cand.params) for s, df in data.items()}
    strategy = cand.strategy(
        StopSizing(protocol["stop_atr_mult"], risk["risk_per_trade_pct"], risk["max_position_pct"])
    )
    costs = Costs.from_config(cfg)
    data_end = min(df["timestamp"].iloc[-1] for df in candles.values()) + HOUR

    lines = [
        "# Stop-fill slippage sensitivity: strategy v1 (candidate A)",
        "",
        "**Sensitivity check, not a new test.** Same candles, splits, parameters and engine as "
        "research round 1; only the slippage on stop fills changes. Entries and signal exits "
        f"stay at {costs.slippage:.2%}; fees stay at {costs.fee:.2%} per side. Test was already "
        "viewed (round 1 second look), so nothing here is out-of-sample evidence.",
        "",
        f"- git commit: {_git_commit()}",
        f"- config sha256: {_sha256(args.config)}",
        f"- protocol sha256: {_sha256(args.protocol)}",
        f"- known gaps sha256: {_sha256(args.known_gaps)}",
        *[f"- data sha256 {s} (1h): {_sha256(p)}" for s, p in paths.items()],
        "",
        "| split | stop slippage | return | Sharpe | max DD | trades | stop exits |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, (start, end) in protocol["splits"].items():
        end_ts = data_end if end is None else pd.Timestamp(end, tz="UTC")
        split = Split(name, pd.Timestamp(start, tz="UTC"), end_ts)
        for mult in MULTIPLIERS:
            c = replace(costs, stop_slippage=costs.slippage * mult)
            engine_cfg = EngineConfig(INITIAL_EQUITY, c, True, RiskLimits.from_config(cfg))
            res = run(data, feats, strategy, engine_cfg, split.start, split.end, step)
            m = _scope_metrics(res, split, "combined", pd.Timedelta(days=365) / step)
            stops = int((res.trades["exit_reason"] == "stop").sum())
            lines.append(
                f"| {name} | {mult}x ({c.stop_slippage:.2%}) | {_pct(m['total_return'])} "
                f"| {_num(m['sharpe'])} | {_pct(m['max_dd'])} | {int(m['trades'])} | {stops} |"
            )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
