"""Pull closed candles via ccxt public endpoints, plus the backfill CLI."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import ccxt
import pandas as pd
import yaml

from trader.data import store

PAGE_LIMIT = 1000
RETRIES = 4


def _step(exchange: ccxt.Exchange, timeframe: str) -> pd.Timedelta:
    return pd.Timedelta(seconds=exchange.parse_timeframe(timeframe))


def _ms(ts: pd.Timestamp) -> int:
    return int(ts.timestamp() * 1000)


def _fetch_page(
    exchange: ccxt.Exchange, symbol: str, timeframe: str, since: int
) -> list[list[float]]:
    for attempt in range(RETRIES):
        try:
            page: list[list[float]] = exchange.fetch_ohlcv(
                symbol, timeframe, since=since, limit=PAGE_LIMIT
            )
            return page
        except ccxt.NetworkError:
            if attempt == RETRIES - 1:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def fetch_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    since: pd.Timestamp,
    until: pd.Timestamp,
) -> pd.DataFrame:
    """Candles with open time in [since, until), paginating forward."""
    step_ms = exchange.parse_timeframe(timeframe) * 1000
    cursor, end = _ms(since), _ms(until)
    rows: list[list[float]] = []
    while cursor < end:
        page = [r for r in _fetch_page(exchange, symbol, timeframe, cursor) if cursor <= r[0] < end]
        if not page:
            break
        rows.extend(page)
        cursor = int(page[-1][0]) + step_ms
    return store.to_frame(rows)


def backfill(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    start: pd.Timestamp,
    data_dir: Path,
    now: pd.Timestamp,
) -> pd.DataFrame:
    """Extend the stored series to cover [start, now), closed candles only. Idempotent."""
    step = _step(exchange, timeframe)
    until = now.floor(step)  # open time of the still-forming candle
    path = store.candle_path(data_dir, symbol, timeframe)
    existing = store.load(path)
    parts = [existing]
    if existing.empty:
        parts.append(fetch_ohlcv(exchange, symbol, timeframe, start, until))
    else:
        first, last = existing["timestamp"].iloc[0], existing["timestamp"].iloc[-1]
        if start < first:
            parts.insert(0, fetch_ohlcv(exchange, symbol, timeframe, start, first))
        parts.append(fetch_ohlcv(exchange, symbol, timeframe, last + step, until))
    df = store.merge(*parts)
    store.save(df, path, step)
    return df


def gap_report(timeframe: str, results: dict[str, tuple[pd.DataFrame, list[store.Gap]]]) -> str:
    lines = [f"# Candle gap report ({timeframe})", ""]
    for symbol, (df, gaps) in results.items():
        span = f"{df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}" if len(df) else "empty"
        lines += [f"## {symbol}", "", f"- candles: {len(df)}", f"- span: {span}"]
        lines.append(f"- gaps: {len(gaps)} ({sum(g.missing for g in gaps)} missing candles)")
        if gaps:
            lines += ["", "| first missing (UTC) | last missing (UTC) | candles |", "|---|---|---|"]
            lines += [f"| {g.start} | {g.end} | {g.missing} |" for g in gaps]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill closed candles for configured pairs.")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument("--since", type=pd.Timestamp, help="UTC start date; overrides --years")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--report", type=Path, default=Path("reports/data_gaps.md"))
    args = parser.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text())
    timeframe: str = cfg["timeframe"]
    exchange = getattr(ccxt, cfg["exchange"])({"enableRateLimit": True})
    step = _step(exchange, timeframe)
    now = pd.Timestamp.now(tz="UTC")
    if args.since is not None:
        start = args.since.tz_localize("UTC") if args.since.tzinfo is None else args.since
    else:
        start = (now - pd.Timedelta(days=365.25 * args.years)).floor(step)

    results: dict[str, tuple[pd.DataFrame, list[store.Gap]]] = {}
    for symbol in cfg["pairs"]:
        df = backfill(exchange, symbol, timeframe, start, args.data_dir, now)
        results[symbol] = (df, store.find_gaps(df, step))
        print(f"{symbol}: {len(df)} candles, {len(results[symbol][1])} gaps")

    report = gap_report(timeframe, results)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report)
    print(f"gap report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
