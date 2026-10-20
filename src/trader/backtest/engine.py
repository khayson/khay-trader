"""Event-driven backtester over a shared hourly timeline and one equity pool.

Per candle i:
  1. at the open: fill orders signalled at the previous close (exits first, then entries),
     priced at the open with slippage and fees; entries are capped by position size,
     total exposure and available cash (inline until Phase 4's risk module);
  2. intrabar stops (optional): if low <= the stop in force, fill at min(open, stop);
  3. at the close: mark to market, then ask the strategy; its intents fill at candle i+1's
     open, never at the close they were computed on. HOLD intents ratchet the stop.
On the window's last candle open positions are liquidated at the close (reason "end").

Documented exchange outages appear as missing candles. Pending orders do not survive a
gap (nobody could trade); held positions carry through and their stop is checked against
the first candle after it, so a gap down fills at that open.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import pandas as pd

from trader.backtest.costs import Costs
from trader.state.snapshot import FEATURES, Snapshot
from trader.strategy.base import Action, Intent, Position, Strategy


@dataclass(frozen=True)
class EngineConfig:
    initial_equity: float
    costs: Costs
    intrabar_stops: bool
    max_position_pct: float
    max_total_exposure_pct: float


@dataclass(frozen=True)
class Result:
    equity: pd.DataFrame  # by close time: equity, cash, exposure, value_<sym>, pnl_<sym>
    fills: pd.DataFrame
    trades: pd.DataFrame


@dataclass
class _Book:
    costs: Costs
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    cashflow: dict[str, float] = field(default_factory=dict)
    open_trades: dict[str, dict[str, Any]] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)

    def value(self, prices: dict[str, float]) -> float:
        return sum(p.qty * prices[s] for s, p in self.positions.items())

    def buy(
        self,
        sym: str,
        qty: float,
        raw: float,
        stop: float,
        time: pd.Timestamp,
        signal_time: pd.Timestamp,
        requested: float,
        equity_at_fill: float,
        opens: dict[str, float],
    ) -> None:
        price = self.costs.buy_price(raw)
        fee = self.costs.fee_on(qty * price)
        cost = qty * price + fee
        self.cash -= cost
        self.cashflow[sym] -= cost
        self.positions[sym] = Position(qty=qty, entry_price=price, stop_price=stop)
        self._fill(
            sym,
            "buy",
            qty,
            raw,
            price,
            fee,
            "entry",
            time,
            signal_time,
            requested=requested,
            equity_at_fill=equity_at_fill,
            exposure_after=self.value(opens),
        )
        self.open_trades[sym] = {
            "symbol": sym,
            "entry_signal_time": signal_time,
            "entry_time": time,
            "entry_price": price,
            "qty": qty,
            "requested_qty": requested,
            "entry_cost": cost,
            "entry_equity": equity_at_fill,
            "fees": fee,
            "slippage": qty * (price - raw),
        }

    def sell(
        self,
        sym: str,
        raw: float,
        reason: str,
        time: pd.Timestamp,
        signal_time: pd.Timestamp | None,
        step: pd.Timedelta,
    ) -> None:
        qty = self.positions.pop(sym).qty
        price = self.costs.stop_sell_price(raw) if reason == "stop" else self.costs.sell_price(raw)
        fee = self.costs.fee_on(qty * price)
        proceeds = qty * price - fee
        self.cash += proceeds
        self.cashflow[sym] += proceeds
        self._fill(sym, "sell", qty, raw, price, fee, reason, time, signal_time)
        t = self.open_trades.pop(sym)
        pnl = proceeds - t["entry_cost"]
        self.trades.append(
            {
                **t,
                "exit_signal_time": signal_time,
                "exit_time": time,
                "exit_price": price,
                "exit_reason": reason,
                "bars_held": int((time - t["entry_time"]) / step),
                "fees": t["fees"] + fee,
                "slippage": t["slippage"] + qty * (raw - price),
                "pnl": pnl,
                "return": pnl / t["entry_cost"],
            }
        )

    def _fill(
        self,
        sym: str,
        side: str,
        qty: float,
        raw: float,
        price: float,
        fee: float,
        reason: str,
        time: pd.Timestamp,
        signal_time: pd.Timestamp | None,
        **extra: float,
    ) -> None:
        self.fills.append(
            {
                "time": time,
                "signal_time": signal_time,
                "symbol": sym,
                "side": side,
                "qty": qty,
                "raw_price": raw,
                "price": price,
                "fee": fee,
                "slippage": qty * abs(price - raw),
                "reason": reason,
                **extra,
            }
        )


def run(
    candles: dict[str, pd.DataFrame],
    features: dict[str, pd.DataFrame],
    strategy: Strategy | Mapping[str, Strategy],
    cfg: EngineConfig,
    start: pd.Timestamp,
    end: pd.Timestamp,
    step: pd.Timedelta,
) -> Result:
    """Simulate candles with open time in [start, end). Features may span more history.

    `strategy` is shared by all symbols, or given per symbol.
    """
    symbols = sorted(candles)
    strategies = strategy if isinstance(strategy, Mapping) else dict.fromkeys(symbols, strategy)
    window = {
        s: df[(df["timestamp"] >= start) & (df["timestamp"] < end)].reset_index(drop=True)
        for s, df in candles.items()
    }
    times = window[symbols[0]]["timestamp"]
    for s in symbols:
        if not window[s]["timestamp"].equals(times):
            raise ValueError(f"{s} candles are not aligned with {symbols[0]}")
    close_times = pd.DatetimeIndex(times + step, name="close_time")
    ohlc = {s: {k: window[s][k].to_numpy() for k in ("open", "low", "close")} for s in symbols}
    feats = {s: features[s].reindex(close_times) for s in symbols}
    feat_arrays = {s: {f: feats[s][f].to_numpy() for f in FEATURES} for s in symbols}
    ready = {s: feats[s]["ready"].fillna(False).to_numpy(dtype=bool) for s in symbols}

    book = _Book(cfg.costs, cfg.initial_equity, cashflow={s: 0.0 for s in symbols})
    pending: dict[str, Intent] = {}
    rows = []
    n = len(times)
    for i in range(n):
        ts = times[i]
        opens = {s: float(ohlc[s]["open"][i]) for s in symbols}
        if i > 0 and ts - times[i - 1] > step:
            pending = {}  # outage: orders signalled before it never reached the market

        # 1. fills at the open for signals from the previous close; exits free cash first
        for s in sorted(pending, key=lambda s: pending[s].action is not Action.EXIT):
            intent = pending[s]
            if intent.action is Action.EXIT and s in book.positions:
                book.sell(s, opens[s], "signal", ts, ts, step)
            elif intent.action is Action.ENTER and s not in book.positions:
                equity = book.cash + book.value(opens)
                price = cfg.costs.buy_price(opens[s])
                notional_cap = min(
                    equity * cfg.max_position_pct / 100,
                    equity * cfg.max_total_exposure_pct / 100 - book.value(opens),
                    book.cash / (1 + cfg.costs.fee),
                )
                qty = min(intent.size, notional_cap / price)
                if qty > 0 and intent.stop_price is not None:
                    book.buy(
                        s, qty, opens[s], intent.stop_price, ts, ts, intent.size, equity, opens
                    )
        pending = {}

        # 2. intrabar stops against the stop in force since the previous close
        if cfg.intrabar_stops:
            for s in sorted(book.positions):
                stop = book.positions[s].stop_price
                if ohlc[s]["low"][i] <= stop:
                    book.sell(s, min(opens[s], stop), "stop", ts, None, step)

        closes = {s: float(ohlc[s]["close"][i]) for s in symbols}
        if i == n - 1:
            for s in sorted(book.positions):
                book.sell(s, closes[s], "end", close_times[i], None, step)

        # 3. mark to market at the close
        equity = book.cash + book.value(closes)
        row: dict[str, float] = {
            "equity": equity,
            "cash": book.cash,
            "exposure": book.value(closes) / equity,
        }
        for s in symbols:
            value = book.positions[s].qty * closes[s] if s in book.positions else 0.0
            row[f"value_{s}"] = value
            row[f"pnl_{s}"] = book.cashflow[s] + value
        rows.append(row)

        # 4. decide at the close; intents fill at the next open
        if i == n - 1:
            break
        for s in symbols:
            if not ready[s][i]:
                continue
            snap = Snapshot(
                time=close_times[i], **{f: float(feat_arrays[s][f][i]) for f in FEATURES}
            )
            position = book.positions.get(s)
            intent = strategies[s].decide(snap, position, equity)
            if intent.action is not Action.HOLD:
                pending[s] = intent
            elif position is not None and intent.stop_price is not None:
                book.positions[s] = replace(position, stop_price=intent.stop_price)

    return Result(
        equity=pd.DataFrame(rows, index=close_times),
        fills=pd.DataFrame(book.fills, columns=_FILL_COLUMNS),
        trades=pd.DataFrame(book.trades, columns=_TRADE_COLUMNS),
    )


_FILL_COLUMNS = [
    "time",
    "signal_time",
    "symbol",
    "side",
    "qty",
    "raw_price",
    "price",
    "fee",
    "slippage",
    "reason",
    "requested",
    "equity_at_fill",
    "exposure_after",
]
_TRADE_COLUMNS = [
    "symbol",
    "entry_signal_time",
    "entry_time",
    "entry_price",
    "qty",
    "requested_qty",
    "entry_cost",
    "entry_equity",
    "exit_signal_time",
    "exit_time",
    "exit_price",
    "exit_reason",
    "bars_held",
    "fees",
    "slippage",
    "pnl",
    "return",
]
