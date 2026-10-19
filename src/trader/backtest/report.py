"""Walk-forward backtest report and CLI.

Features are computed once over the full history and the engine is run per split, so
validate/test use warm, causal features. The test split is locked unless --unlock-test
is passed; run it once, at the end.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from trader.backtest import metrics
from trader.backtest.costs import ZERO, Costs
from trader.backtest.engine import EngineConfig, Result, run
from trader.data import store
from trader.state.snapshot import FeatureParams, features
from trader.strategy.baseline import Baseline, BaselineParams

INITIAL_EQUITY = 10_000.0
VARIANTS = {"intrabar-stop": True, "close-stop": False}  # first is the primary result
SMALL_SAMPLE = 30

__all__ = ["Costs", "Meta", "Split", "build_report", "make_splits", "random_entry_returns"]


@dataclass(frozen=True)
class Split:
    name: str
    start: pd.Timestamp  # first candle open time (inclusive)
    end: pd.Timestamp  # candle open time bound (exclusive)


@dataclass(frozen=True)
class Meta:
    date: str
    git_commit: str
    config_sha256: str
    data_sha256: dict[str, str]
    seed: int


def make_splits(first: pd.Timestamp, end: pd.Timestamp) -> list[Split]:
    """Train: first 12 months. Validate: next 6. Test: the rest (about 6 months)."""
    a = first + pd.DateOffset(months=12)
    b = first + pd.DateOffset(months=18)
    return [Split("train", first, a), Split("validate", a, b), Split("test", b, end)]


def random_entry_returns(
    opens: np.ndarray, trades: pd.DataFrame, costs: Costs, rng: np.random.Generator, n_runs: int
) -> np.ndarray:
    """Total returns of n_runs random-entry runs on one pair.

    Each run reuses the strategy's trades' holding times and equity weights, shuffled and
    placed at uniformly random non-overlapping entry opens; exits at the open h bars later.
    """
    out = np.zeros(n_runs)
    if trades.empty:
        return out
    holds = np.maximum(1, trades["bars_held"].to_numpy(dtype=int))
    weights = (trades["entry_cost"] / trades["entry_equity"]).to_numpy(dtype=float)
    free = len(opens) - 1 - holds.sum()
    if free < 0:
        raise ValueError("strategy holding times exceed the window")
    round_trip = (1 - costs.slippage) * (1 - costs.fee) / ((1 + costs.slippage) * (1 + costs.fee))
    for k in range(n_runs):
        order = rng.permutation(len(holds))
        h, w = holds[order], weights[order]
        starts = np.sort(rng.integers(0, free + 1, size=len(h))) + np.concatenate(
            [[0], np.cumsum(h)[:-1]]
        )
        r = opens[starts + h] / opens[starts] * round_trip - 1
        out[k] = np.expm1(np.log1p(w * r).sum())
    return out


def buy_and_hold(
    candles: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp,
    weight: float,
    costs: Costs,
    step: pd.Timedelta,
) -> pd.Series:
    """Equity of holding `weight` of equity split equally across pairs, bought at the first
    open and sold at the last close, with costs. Indexed by close time plus a start point."""
    cash = INITIAL_EQUITY
    held = []
    for df in candles.values():
        w = df[(df["timestamp"] >= start) & (df["timestamp"] < end)]
        price = costs.buy_price(float(w["open"].iloc[0]))
        qty = INITIAL_EQUITY * weight / len(candles) / (price * (1 + costs.fee))
        cash -= qty * price * (1 + costs.fee)
        held.append((qty, w["close"].to_numpy()))
        index = pd.DatetimeIndex(w["timestamp"] + step)
    equity: np.ndarray = cash + np.sum([q * c for q, c in held], axis=0)
    exit_value = sum(q * costs.sell_price(c[-1]) * (1 - costs.fee) for q, c in held)
    equity[-1] = cash + exit_value
    curve = pd.Series(equity, index=index)
    out: pd.Series = pd.concat([pd.Series([INITIAL_EQUITY], index=[start]), curve])
    return out


def _scope_metrics(res: Result, split: Split, scope: str) -> dict[str, float]:
    eq = res.equity
    if scope == "combined":
        curve, trades = eq["equity"], res.trades
        exposure = eq["exposure"].mean()
    else:
        curve, trades = (
            INITIAL_EQUITY + eq[f"pnl_{scope}"],
            res.trades[res.trades["symbol"] == scope],
        )
        exposure = (eq[f"value_{scope}"] / eq["equity"]).mean()
    curve = pd.concat([pd.Series([INITIAL_EQUITY], index=[split.start]), curve])
    total = curve.iloc[-1] / INITIAL_EQUITY - 1
    return {
        "total_return": total,
        "cagr": metrics.cagr(total, split.end - split.start),
        "sharpe": metrics.sharpe(curve),
        "max_dd": metrics.max_drawdown(curve),
        **metrics.trade_stats(trades["return"], trades["pnl"]),
        "exposure": exposure,
        "fees": trades["fees"].sum(),
        "slippage": trades["slippage"].sum(),
    }


def _pct(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{100 * x:.2f}%"


def _num(x: float) -> str:
    return "n/a" if math.isnan(x) else ("inf" if math.isinf(x) else f"{x:.2f}")


def _metrics_table(rows: list[tuple[str, str, str, dict[str, float]]]) -> list[str]:
    out = [
        "| variant | costs | scope | return | CAGR | Sharpe | max DD | trades | win | PF "
        "| avg trade | exposure | fees | slippage |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for variant, costs, scope, m in rows:
        out.append(
            f"| {variant} | {costs} | {scope} | {_pct(m['total_return'])} | {_pct(m['cagr'])} "
            f"| {_num(m['sharpe'])} | {_pct(m['max_dd'])} | {int(m['trades'])} "
            f"| {_pct(m['win_rate'])} | {_num(m['profit_factor'])} | {_pct(m['avg_trade'])} "
            f"| {_pct(m['exposure'])} | {m['fees']:.2f} | {m['slippage']:.2f} |"
        )
    return out


def build_report(
    candles: dict[str, pd.DataFrame],
    cfg: dict[str, Any],
    meta: Meta,
    include_test: bool,
    n_mc: int = 1000,
) -> tuple[str, dict[str, pd.DataFrame]]:
    step = pd.Timedelta(cfg["timeframe"])
    symbols = sorted(candles)
    feats = {s: features(candles[s], step, FeatureParams.from_config(cfg)) for s in symbols}
    strategy = Baseline(BaselineParams.from_config(cfg))
    costs = Costs.from_config(cfg)
    first = max(df["timestamp"].iloc[0] for df in candles.values())
    end = min(df["timestamp"].iloc[-1] for df in candles.values()) + step
    first_ready = max(f.index[f["ready"]][0] for f in feats.values()) - step  # candle open time
    splits = make_splits(first, end)
    risk = cfg["risk"]

    lines = [
        f"# Backtest report {meta.date}",
        "",
        "## Reproducibility",
        "",
        f"- git commit: {meta.git_commit}",
        f"- config sha256: {meta.config_sha256}",
        f"- seed: {meta.seed} (Monte Carlo runs: {n_mc}; bootstrap resamples: 10000)",
        *[f"- data sha256 {s}: {meta.data_sha256[s]}" for s in symbols],
        f"- data span (candle open times): {first} -> {end - step}",
        f"- first ready candle (warmup {FeatureParams.from_config(cfg).warmup_bars(step)} bars): "
        f"{first_ready}",
        *[f"- {sp.name}: [{sp.start}, {sp.end}) candle open times" for sp in splits],
        "",
        "## Setup",
        "",
        "- Strategy: baseline, config defaults, not tuned. No sensitivity grid was run.",
        f"- Initial equity {INITIAL_EQUITY:,.0f} per split, flat at split start. Fees "
        f"{costs.fee:.4%}/side, slippage {costs.slippage:.4%}/side when costs are on.",
        "- Signals on candle close fill at the next candle's open. intrabar-stop: a low at or "
        "below the stop fills at min(open, stop). close-stop: stops act only via the "
        "strategy's close check, filling at the next open.",
        f"- Caps enforced inline at fill: position {risk['max_position_pct']}%, total exposure "
        f"{risk['max_total_exposure_pct']}%, no negative cash.",
        "- Open positions are closed at the last close of each split (exit reason `end`).",
        "- Baselines (costs on) start at the first ready candle of each split: buy-and-hold of "
        "both pairs at the strategy's average exposure; random entry reusing the strategy's "
        "holding times and position weights at random non-overlapping entry times.",
        "",
    ]
    if not include_test:
        lines += ["**Test split LOCKED: not run.** Pass --unlock-test once, at the end.", ""]
        splits = splits[:2]

    trades_out: dict[str, list[pd.DataFrame]] = {v: [] for v in VARIANTS}
    for split_idx, split in enumerate(splits):
        active = max(split.start, first_ready)
        results = {
            (v, on): run(
                candles,
                feats,
                strategy,
                EngineConfig(
                    INITIAL_EQUITY,
                    costs if on else ZERO,
                    intrabar,
                    risk["max_position_pct"],
                    risk["max_total_exposure_pct"],
                ),
                split.start,
                split.end,
                step,
            )
            for v, intrabar in VARIANTS.items()
            for on in (True, False)
        }
        rows = [
            (v, "on" if on else "off", scope, _scope_metrics(results[(v, on)], split, scope))
            for v in VARIANTS
            for on in (True, False)
            for scope in ["combined", *symbols]
        ]
        lines += [
            f"## {split.name}",
            "",
            f"Window [{split.start}, {split.end}); strategy can trade from {active}.",
            "",
            *_metrics_table(rows),
            "",
        ]

        lines += [
            "### Baselines and significance (costs on)",
            "",
            "| variant | strategy return | buy-and-hold @ same exposure | B&H Sharpe "
            "| B&H max DD | random entry p5 / median / p95 | strategy percentile "
            "| avg trade 95% CI | capped entries |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        notes = []
        for v_idx, v in enumerate(VARIANTS):
            res = results[(v, True)]
            trades_out[v].append(res.trades.assign(split=split.name))
            strat_total = res.equity["equity"].iloc[-1] / INITIAL_EQUITY - 1
            weight = float(res.equity.loc[res.equity.index > active, "exposure"].mean())
            bh = buy_and_hold(candles, active, split.end, weight, costs, step)
            rng = np.random.default_rng([meta.seed, split_idx, v_idx])
            mc = np.ones(n_mc)
            for s in symbols:
                df = candles[s]
                opens = df.loc[
                    (df["timestamp"] >= active) & (df["timestamp"] < split.end), "open"
                ].to_numpy()
                t = res.trades[res.trades["symbol"] == s]
                mc *= 1 + random_entry_returns(opens, t, costs, rng, n_mc)
            mc -= 1
            n = len(res.trades)
            if n >= 2:
                lo, hi = metrics.bootstrap_ci(res.trades["return"], seed=meta.seed)
                ci = f"[{_pct(lo)}, {_pct(hi)}]"
            else:
                ci = "n/a"
            capped = (res.trades["qty"] < res.trades["requested_qty"] * (1 - 1e-9)).mean()
            lines.append(
                f"| {v} | {_pct(strat_total)} | {_pct(bh.iloc[-1] / INITIAL_EQUITY - 1)} "
                f"| {_num(metrics.sharpe(bh))} | {_pct(metrics.max_drawdown(bh))} "
                f"| {_pct(float(np.percentile(mc, 5)))} / {_pct(float(np.median(mc)))} / "
                f"{_pct(float(np.percentile(mc, 95)))} | {100 * (mc < strat_total).mean():.1f} "
                f"| {ci} | {_pct(capped) if n else 'n/a'} |"
            )
            if n < SMALL_SAMPLE:
                notes.append(f"- {v}: only {n} trades. Too few to conclude anything about edge.")
        lines += ["", *notes, ""] if notes else [""]

    trades = {v: pd.concat(parts, ignore_index=True) for v, parts in trades_out.items()}
    return "\n".join(lines), trades


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{head} (uncommitted changes present)" if dirty else head


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward backtest of the baseline.")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("reports"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mc-runs", type=int, default=1000)
    parser.add_argument(
        "--unlock-test",
        action="store_true",
        help="also run the locked test split; do this once, at the end",
    )
    args = parser.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text())
    paths = {s: store.candle_path(args.data_dir, s, cfg["timeframe"]) for s in cfg["pairs"]}
    candles = {s: store.load(p) for s, p in paths.items()}
    date = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    meta = Meta(
        date,
        _git_commit(),
        _sha256(args.config),
        {s: _sha256(p) for s, p in paths.items()},
        args.seed,
    )

    md, trades = build_report(candles, cfg, meta, args.unlock_test, args.mc_runs)
    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / f"backtest_{date}.md"
    report_path.write_text(md)
    for v, df in trades.items():
        df.to_csv(args.out / f"backtest_{date}_trades_{v}.csv", index=False)
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
