"""Research round 1: pre-registered candidates, walk-forward evaluation and decision rule.

The protocol (candidates, splits, rule, seed) lives in config/research_round1.yaml and must
not change after a run. Features are computed once per candidate over the full resampled
history and sliced per split. Decisions use 1x costs and the intrabar-stop engine only.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from trader.backtest import metrics
from trader.backtest.costs import Costs
from trader.backtest.engine import EngineConfig, run
from trader.backtest.report import (
    INITIAL_EQUITY,
    SMALL_SAMPLE,
    Meta,
    Split,
    _git_commit,
    _metrics_table,
    _num,
    _pct,
    _scope_metrics,
    _sha256,
    buy_and_hold,
)
from trader.data import store
from trader.data.resample import resample
from trader.risk.limits import RiskLimits
from trader.state.snapshot import FeatureParams, Snapshot, features
from trader.strategy.base import (
    Action,
    Intent,
    Position,
    StopSizing,
    Strategy,
    TrailingStopStrategy,
    position_size,
)
from trader.strategy.baseline import Baseline, BaselineParams
from trader.strategy.candidates import Breakout, EmaTrend

HOUR = pd.Timedelta(hours=1)


@dataclass(frozen=True)
class SplitScore:
    net_return: float
    percentile: float  # % of random-entry runs with a lower return
    sharpe: float
    max_dd: float  # negative fraction


def passes(s: SplitScore, min_percentile: float) -> bool:
    return s.net_return > 0 and s.percentile >= min_percentile


def advancing(scores: dict[str, dict[str, SplitScore]], min_percentile: float) -> str | None:
    """At most one candidate: passes on train AND validate, best validate Sharpe."""
    ok = [
        name
        for name, by_split in scores.items()
        if passes(by_split["train"], min_percentile)
        and passes(by_split["validate"], min_percentile)
    ]
    return max(ok, key=lambda n: scores[n]["validate"].sharpe) if ok else None


def go(s: SplitScore, min_percentile: float, max_dd_pct: float) -> bool:
    return passes(s, min_percentile) and -s.max_dd * 100 <= max_dd_pct


# --- candidates --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    name: str
    timeframe: str
    rule: str
    params: FeatureParams

    @classmethod
    def from_protocol(cls, name: str, spec: dict[str, Any], protocol: dict[str, Any]) -> Candidate:
        common = {
            "atr_period": protocol["atr_period"],
            "vol_window_hours": 168,
            "drawdown_window_hours": 168,
        }
        if spec["rule"] == "ema_trend":
            # Channels are unused; 20 bars keeps them below ema_slow so warmup = 5 x ema_slow.
            params = FeatureParams(
                ema_fast=spec["ema_fast"],
                ema_slow=spec["ema_slow"],
                channel_entry=20,
                channel_exit=20,
                **common,
            )
        elif spec["rule"] == "breakout":
            # EMAs are unused; 20 bars keeps them below channel_entry so warmup = 5 x channel.
            params = FeatureParams(
                ema_fast=20,
                ema_slow=20,
                channel_entry=spec["channel_entry"],
                channel_exit=spec["channel_exit"],
                **common,
            )
        else:
            raise ValueError(f"unknown rule {spec['rule']}")
        return cls(name, spec["timeframe"], spec["rule"], params)

    def strategy(self, sizing: StopSizing) -> TrailingStopStrategy:
        return EmaTrend(sizing) if self.rule == "ema_trend" else Breakout(sizing)

    def describe(self) -> str:
        p = self.params
        if self.rule == "ema_trend":
            return (
                f"{self.timeframe} bars, EMA {p.ema_fast}/{p.ema_slow}: enter close > fast > "
                f"slow, exit close < slow"
            )
        return (
            f"{self.timeframe} bars, enter close > prior {p.channel_entry}-bar high, exit "
            f"close < prior {p.channel_exit}-bar low"
        )


# --- random entry through the engine -----------------------------------------------------------


def random_schedule(
    signal_times: pd.DatetimeIndex, holds: np.ndarray, rng: np.random.Generator
) -> tuple[set[pd.Timestamp], set[pd.Timestamp]]:
    """Shuffle holding times (in bars) and place them at uniformly random, non-overlapping
    positions among `signal_times`. Returns entry and exit signal times; each entry comes
    strictly after the previous exit."""
    if len(holds) == 0:
        return set(), set()
    h = holds[rng.permutation(len(holds))]
    free = len(signal_times) - int((h + 1).sum())
    if free < 0:
        raise ValueError("holding times exceed the window")
    starts = np.sort(rng.integers(0, free + 1, size=len(h))) + np.concatenate(
        [[0], np.cumsum(h + 1)[:-1]]
    )
    return set(signal_times[starts]), set(signal_times[starts + h])


class RandomEntry:
    """Enters at scheduled signal times with the candidate's sizing; exits on schedule only."""

    def __init__(self, entries: set[pd.Timestamp], exits: set[pd.Timestamp], sizing: StopSizing):
        self.entries, self.exits, self.sizing = entries, exits, sizing

    def decide(self, snap: Snapshot, position: Position | None, equity: float) -> Intent:
        if position is None and snap.time in self.entries:
            stop = snap.close - self.sizing.stop_atr_mult * snap.atr
            size = position_size(equity, snap.close, stop, self.sizing) if stop > 0 else 0.0
            # stop_price 0: no stop exits, so holding times match the schedule
            return (
                Intent(Action.ENTER, size=size, stop_price=0.0) if size > 0 else Intent(Action.HOLD)
            )
        if position is not None and snap.time in self.exits:
            return Intent(Action.EXIT, size=position.qty)
        return Intent(Action.HOLD)


def random_entry_returns(
    data: dict[str, pd.DataFrame],
    feats: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    cfg: EngineConfig,
    sizing: StopSizing,
    split: Split,
    step: pd.Timedelta,
    rng: np.random.Generator,
    n_runs: int,
) -> np.ndarray:
    """Total returns of n_runs random-entry runs through the engine (shared equity pool)."""
    signal_times = {}
    holds = {}
    for s, f in feats.items():
        index = pd.DatetimeIndex(f.index)
        close_times = index[(index - step >= split.start) & (index - step < split.end)]
        ready = f.loc[close_times, "ready"].to_numpy(dtype=bool)
        signal_times[s] = close_times[ready][:-1]  # no decision on the window's last candle
        t = trades[trades["symbol"] == s]
        holds[s] = np.maximum(1, t["bars_held"].to_numpy(dtype=int))
    out = np.zeros(n_runs)
    for k in range(n_runs):
        strategies: dict[str, Strategy] = {}
        for s in sorted(feats):
            entries, exits = random_schedule(signal_times[s], holds[s], rng)
            strategies[s] = RandomEntry(entries, exits, sizing)
        res = run(data, feats, strategies, cfg, split.start, split.end, step)
        out[k] = res.equity["equity"].iloc[-1] / INITIAL_EQUITY - 1
    return out


# --- report ------------------------------------------------------------------------------------


def _splits(protocol: dict[str, Any], data_end: pd.Timestamp) -> list[Split]:
    out = []
    for name, (start, end) in protocol["splits"].items():
        end_ts = data_end if end is None else pd.Timestamp(end, tz="UTC")
        out.append(Split(name, pd.Timestamp(start, tz="UTC"), end_ts))
    return out


def _baseline_rows(
    candles_1h: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
    exposure: float,
    costs: Costs,
) -> list[tuple[str, pd.Series]]:
    rows = [
        (f"buy-and-hold {s} 100%", buy_and_hold({s: candles_1h[s]}, start, end, 1.0, costs, HOUR))
        for s in sorted(candles_1h)
    ]
    rows.append(("buy-and-hold 50/50", buy_and_hold(candles_1h, start, end, 1.0, costs, HOUR)))
    rows.append(
        (
            f"buy-and-hold 50/50 @ {_pct(exposure)} (exposure-matched)",
            buy_and_hold(candles_1h, start, end, exposure, costs, HOUR),
        )
    )
    return rows


def build_research_report(
    candles_1h: dict[str, pd.DataFrame],
    cfg: dict[str, Any],
    protocol: dict[str, Any],
    outages: Mapping[str, Sequence[store.Gap]],
    meta: Meta,
    unlock_test: bool,
    extra_meta: Sequence[str] = (),
) -> tuple[str, dict[str, pd.DataFrame]]:
    symbols = sorted(candles_1h)
    costs = Costs.from_config(cfg)
    half = Costs(
        costs.fee * protocol["cost_sensitivity"], costs.slippage * protocol["cost_sensitivity"]
    )
    risk = cfg["risk"]
    sizing = StopSizing(
        protocol["stop_atr_mult"], risk["risk_per_trade_pct"], risk["max_position_pct"]
    )
    rule = protocol["rule"]
    n_mc = protocol["mc_runs"]
    data_end = min(df["timestamp"].iloc[-1] for df in candles_1h.values()) + HOUR
    splits = {sp.name: sp for sp in _splits(protocol, data_end)}
    candidates = [
        Candidate.from_protocol(n, spec, protocol) for n, spec in protocol["candidates"].items()
    ]

    def engine_cfg(c: Costs) -> EngineConfig:
        return EngineConfig(INITIAL_EQUITY, c, True, RiskLimits.from_config(cfg))

    lines = [
        f"# Research round 1 ({meta.date})",
        "",
        "## Reproducibility",
        "",
        f"- git commit: {meta.git_commit}",
        f"- config sha256: {meta.config_sha256}",
        *extra_meta,
        f"- seed: {meta.seed} (random-entry runs per candidate and split: {n_mc}; bootstrap "
        "resamples: 10000)",
        *[f"- data sha256 {s} (1h): {meta.data_sha256[s]}" for s in symbols],
        *[f"- {sp.name}: [{sp.start}, {sp.end})" for sp in splits.values()],
        "",
        "## Protocol",
        "",
        "Pre-registered in config/research_round1.yaml. Exactly four candidates, all long-only "
        f"spot, ATR({protocol['atr_period']}) x{protocol['stop_atr_mult']} initial and "
        "ratcheting stop with intrabar fills, sizing/caps/costs as config/default.yaml "
        f"(fee {costs.fee:.2%} + slippage {costs.slippage:.2%} per side), warmup 5x the longest "
        "lookback in bars:",
        "",
        *[
            f"- **{c.name}**: {c.describe()} "
            f"(warmup {c.params.warmup_bars(pd.Timedelta(c.timeframe))} bars)"
            for c in candidates
        ],
        "",
        f"Advance rule: net return after 1x costs > 0 AND >= {rule['min_percentile']}th "
        "percentile of random entry, on BOTH train and validate; among those, highest validate "
        "Sharpe; at most one advances. Go on test (second look, once): same two conditions plus "
        f"max drawdown <= {rule['test_max_drawdown_pct']}%.",
        "",
        "**Multiple comparisons:** four candidates were tried. Even with no real edge, the "
        "chance that at least one clears an 80th-percentile bar on one split by luck is far "
        "higher than for a single pre-planned test, so a pass here is weaker evidence than it "
        "looks.",
        "",
        "**Deviation from the round-1 prompt (approved by the user):** the 27 Binance-wide "
        "outages in config/known_gaps.yaml are treated as market-closed time. Features count "
        "bars across them, resampled bars are built from the hours that traded (other "
        "incomplete bars are dropped), orders signalled before an outage are cancelled, and "
        "stops are checked at the first open after it. Restarting warmup at every outage would "
        "have left C with no tradeable train days and B/D with 10-16%.",
        "",
        "Baselines run on 1h candles over each candidate's tradeable window. Random entry "
        "reuses the candidate's per-pair holding times, shuffled and placed at random "
        "non-overlapping times, with the same sizing, through the engine's shared equity pool. "
        "Percentile = share of random runs with a lower return.",
        "",
    ]

    trades_out: dict[str, list[pd.DataFrame]] = {c.name: [] for c in candidates}
    scores: dict[str, dict[str, SplitScore]] = {c.name: {} for c in candidates}
    prepared = {}
    for c in candidates:
        step = pd.Timedelta(c.timeframe)
        data = {s: resample(candles_1h[s], c.timeframe, HOUR, outages.get(s, ())) for s in symbols}
        feats = {s: features(data[s], step, c.params) for s in symbols}
        prepared[c.name] = (step, data, feats)

    def evaluate(c: Candidate, split: Split, split_idx: int) -> list[str]:
        step, data, feats = prepared[c.name]
        periods = pd.Timedelta(days=365) / step
        strategy = c.strategy(sizing)
        first_ready = max(f.index[f["ready"]][0] for f in feats.values()) - step
        active = max(split.start, first_ready)
        res = run(data, feats, strategy, engine_cfg(costs), split.start, split.end, step)
        res_half = run(data, feats, strategy, engine_cfg(half), split.start, split.end, step)
        trades_out[c.name].append(res.trades.assign(split=split.name))
        rows = [
            (c.name, label, scope, _scope_metrics(r, split, scope, periods))
            for label, r in (("1x", res), (f"{protocol['cost_sensitivity']}x", res_half))
            for scope in ["combined", *symbols]
        ]
        rng = np.random.default_rng([meta.seed, split_idx, ord(c.name)])
        mc = random_entry_returns(
            data, feats, res.trades, engine_cfg(costs), sizing, split, step, rng, n_mc
        )
        net = res.equity["equity"].iloc[-1] / INITIAL_EQUITY - 1
        combined = rows[0][3]
        pct = float(100 * (mc < net).mean())
        scores[c.name][split.name] = SplitScore(net, pct, combined["sharpe"], combined["max_dd"])
        exposure = float(res.equity.loc[res.equity.index > active, "exposure"].mean())
        out = [
            f"### {c.name} / {split.name}",
            "",
            f"Tradeable from {active} (warmup); window ends {split.end}.",
            "",
            *_metrics_table(rows),
            "",
            "| comparison (1x costs, tradeable window) | return | Sharpe | max DD |",
            "|---|---|---|---|",
            f"| **candidate {c.name}** | {_pct(net)} | {_num(combined['sharpe'])} "
            f"| {_pct(combined['max_dd'])} |",
        ]
        for label, curve in _baseline_rows(candles_1h, active, split.end, exposure, costs):
            out.append(
                f"| {label} | {_pct(curve.iloc[-1] / INITIAL_EQUITY - 1)} "
                f"| {_num(metrics.sharpe(curve))} | {_pct(metrics.max_drawdown(curve))} |"
            )
        out.append(
            f"| random entry p5 / median / p95 ({n_mc} runs) | "
            f"{_pct(float(np.percentile(mc, 5)))} / {_pct(float(np.median(mc)))} / "
            f"{_pct(float(np.percentile(mc, 95)))} | | |"
        )
        n = len(res.trades)
        ci = "n/a"
        if n >= 2:
            lo, hi = metrics.bootstrap_ci(res.trades["return"], seed=meta.seed)
            ci = f"[{_pct(lo)}, {_pct(hi)}]"
        out += [
            "",
            f"Random-entry percentile: **{pct:.1f}**. Average trade 95% CI: {ci}. Trades: {n}.",
        ]
        if n < SMALL_SAMPLE:
            out.append(f"**Only {n} trades: too few to conclude anything about edge.**")
        out.append("")
        return out

    split_names = ["train", "validate"]
    for c in candidates:
        lines += [f"## Candidate {c.name}", "", c.describe(), ""]
        for idx, name in enumerate(split_names):
            lines += evaluate(c, splits[name], idx)

    # Reference: the Phase 3 1h baseline on the new splits (no random-entry runs).
    ref_feats = {s: features(candles_1h[s], HOUR, FeatureParams.from_config(cfg)) for s in symbols}
    ref = Baseline(BaselineParams.from_config(cfg))
    ref_rows = []
    for name in split_names:
        sp = splits[name]
        r = run(candles_1h, ref_feats, ref, engine_cfg(costs), sp.start, sp.end, HOUR)
        ref_rows.append(
            ("1h baseline", f"1x {name}", "combined", _scope_metrics(r, sp, "combined"))
        )
    lines += [
        "## Reference: Phase 3 baseline (1h, EMA 50/200, ATR% band)",
        "",
        *_metrics_table(ref_rows),
        "",
    ]

    lines += [
        "## Decision",
        "",
        "| candidate | train net | train pct | validate net | validate pct | validate Sharpe "
        "| passes |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in candidates:
        tr, va = scores[c.name]["train"], scores[c.name]["validate"]
        ok = passes(tr, rule["min_percentile"]) and passes(va, rule["min_percentile"])
        lines.append(
            f"| {c.name} | {_pct(tr.net_return)} | {tr.percentile:.1f} "
            f"| {_pct(va.net_return)} | {va.percentile:.1f} | {_num(va.sharpe)} "
            f"| {'yes' if ok else 'no'} |"
        )
    winner = advancing(scores, rule["min_percentile"])
    lines.append("")
    if winner is None:
        lines += [
            "**No candidate advances. STOP: strategy research ends here per the "
            "pre-registered rule. The test split stays LOCKED and was not run.**",
            "",
        ]
    elif not unlock_test:
        lines += [
            f"**Candidate {winner} advances.** Test split LOCKED: rerun with --unlock-test "
            "to run the one-time second look for this candidate only.",
            "",
        ]
    else:
        c = next(x for x in candidates if x.name == winner)
        lines += [
            f"## Test (second look): candidate {winner}",
            "",
            "This period was already used once by the Phase 3 1h baseline.",
            "",
        ]
        lines += evaluate(c, splits["test"], 2)
        t = scores[winner]["test"]
        verdict = go(t, rule["min_percentile"], rule["test_max_drawdown_pct"])
        lines += [
            f"**{'GO' if verdict else 'NO GO'}**: net {_pct(t.net_return)}, percentile "
            f"{t.percentile:.1f}, max drawdown {_pct(t.max_dd)}.",
            "",
        ]

    trades = {name: pd.concat(parts, ignore_index=True) for name, parts in trades_out.items()}
    return "\n".join(lines), trades


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research round 1 (pre-registered).")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--protocol", type=Path, default=Path("config/research_round1.yaml"))
    parser.add_argument("--known-gaps", type=Path, default=Path("config/known_gaps.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("reports"))
    parser.add_argument(
        "--unlock-test",
        action="store_true",
        help="run the one-time test second look if a candidate advances",
    )
    args = parser.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text())
    protocol = yaml.safe_load(args.protocol.read_text())
    paths = {s: store.candle_path(args.data_dir, s, "1h") for s in cfg["pairs"]}
    candles = {s: store.load(p) for s, p in paths.items()}
    outages = {s: store.load_known_gaps(args.known_gaps, s, "1h") for s in cfg["pairs"]}
    date = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    meta = Meta(
        date,
        _git_commit(),
        _sha256(args.config),
        {s: _sha256(p) for s, p in paths.items()},
        protocol["seed"],
    )
    extra = [
        f"- protocol sha256: {hashlib.sha256(args.protocol.read_bytes()).hexdigest()}",
        f"- known gaps sha256: {_sha256(args.known_gaps)}",
    ]
    md, trades = build_research_report(
        candles, cfg, protocol, outages, meta, args.unlock_test, extra
    )
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"research_round1_{date}.md"
    path.write_text(md)
    for name, df in trades.items():
        df.to_csv(args.out / f"research_round1_{date}_trades_{name}.csv", index=False)
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
