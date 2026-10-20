"""Paper-trading loop for strategy v1 (SPEC section 5), paper only.

One wake per 4h bar close (UTC), `wake_delay_seconds` after it, plus a tick every minute
in between. A wake mirrors the backtest engine's per-bar order, so a replay reproduces the
engine trade for trade:

  1. refresh 1h candles (public endpoints); confirm and persist any new missing hours as
     outages (market-closed time, like config/known_gaps.yaml); build 4h bars with the
     backtest's resample() and features();
  2. resting stops: fills on 1m candles up to the newest bar's close;
  3. mark equity at the newest closed bar; kill switch on_equity; journal the wake slot
     (processed | late | missed | stale | outage);
  4. decide on the newest bar (once per bar). HOLD ratchets the stop; exits always run,
     even on a late wake; entries need pre_trade_check (freshness, daily loss, rate,
     caps) and are also skipped on clock skew, a newest-bar mismatch with the exchange's
     native klines, a runtime outage in the decision bar, too many missed wakes, or an
     order below the exchange minimum;
  5. execute at the current 1h candle's open (= the new bar's open on an on-time wake):
     exits first, then entries; a stop is cancelled only after its position's sell fills;
  6. kill switch tripped: entries blocked; with flatten_on_trip, market-sell everything;
  7. after trading (never blocking it): full parity sweep, once-a-day divergence check
     and summary, dead-man's-switch ping.

Minute ticks poll the simulated stops (never past the newest decided bar, so outcomes
match the replay) and flatten promptly after a trip. All state lives in the SQLite
journal; each wake and tick commits atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any

import ccxt
import pandas as pd
import yaml

from trader.backtest.costs import Costs
from trader.backtest.research import Candidate
from trader.data import store
from trader.data.fetch import backfill, fetch_ohlcv, retry_call
from trader.data.resample import resample
from trader.exec import divergence
from trader.exec.alerts import Alerter
from trader.exec.base import Broker, Fill
from trader.exec.journal import Journal, PositionRow
from trader.exec.paper import PaperBroker
from trader.logging_setup import setup_logging
from trader.risk import killswitch as ks
from trader.risk.limits import (
    Account,
    EntryOrder,
    Rejected,
    RiskLimits,
    entry_headroom,
    overcap_symbols,
    pre_trade_check,
)
from trader.state.snapshot import FEATURES, Snapshot, features
from trader.strategy.base import Action, Intent, Position, StopSizing

log = logging.getLogger("trader.loop")
HOUR = pd.Timedelta(hours=1)
MINUTE = pd.Timedelta(minutes=1)
MAX_CRASHES = 5
STALE_GRACE = pd.Timedelta(minutes=15)  # wait this long for a late bar before going stale
STALE_RETRY = pd.Timedelta(minutes=5)  # then keep retrying a stale slot this often

# Entry rejections the backtest cannot reproduce; divergence treats their bars as unclean.
OPERATIONAL_REJECTIONS = (
    "rejected: stale data",
    "rejected: kill switch",
    "rejected: exchange minimum",
    "rejected: clock",
    "rejected: parity",
    "rejected: outage",
    "rejected: missed wakes",
)


class MarketDataError(Exception):
    """Exchange data missing or inconsistent; handled like an API error."""


class EquityMismatch(Exception):
    """Configured initial equity differs from the one this journal was started with."""


class StaleFeedRetry(MarketDataError):
    """The newest bar is not published yet; retry the wake (not an API error)."""


@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...]
    candidate: Candidate
    sizing: StopSizing
    limits: RiskLimits
    ks_limits: ks.KillSwitchLimits
    ks_path: Path
    costs: Costs  # fee, slippage and stop_slippage
    initial_equity: float
    flatten_on_trip: bool
    data_dir: Path
    outages: Mapping[str, Sequence[store.Gap]]  # documented (known_gaps.yaml)
    history_hours: int
    parity_bars: int
    divergence_min_fills: int
    divergence_slippage_mult: float
    wake_delay_seconds: float = 10.0
    retry_delays: tuple[float, ...] = (5.0, 25.0)
    clock_skew_max_seconds: float = 2.0
    missed_wake_max: int = 2
    missed_wake_window_days: int = 7

    @classmethod
    def from_config(cls, cfg: dict[str, Any], root: Path = Path(".")) -> Settings:
        paper, risk = cfg["paper"], cfg["risk"]
        protocol = yaml.safe_load((root / paper["strategy_protocol"]).read_text())
        name = paper["strategy_candidate"]
        candidate = Candidate.from_protocol(name, protocol["candidates"][name], protocol)
        gaps = root / paper["known_gaps"]
        equity = os.environ.get("TRADER_PAPER_EQUITY") or paper["initial_equity"]
        return cls(
            symbols=tuple(cfg["pairs"]),
            candidate=candidate,
            sizing=StopSizing(
                protocol["stop_atr_mult"], risk["risk_per_trade_pct"], risk["max_position_pct"]
            ),
            limits=RiskLimits.from_config(cfg),
            ks_limits=ks.KillSwitchLimits.from_config(cfg),
            ks_path=root / risk["killswitch_state"],
            costs=replace(Costs.from_config(cfg), stop_slippage=paper["stop_slippage"]),
            initial_equity=float(equity),
            flatten_on_trip=bool(risk["flatten_on_trip"]),
            data_dir=root / paper["data_dir"],
            outages={s: store.load_known_gaps(gaps, s, "1h") for s in cfg["pairs"]},
            history_hours=int(paper["history_hours"]),
            parity_bars=int(paper["parity_bars"]),
            divergence_min_fills=int(paper["divergence_min_fills"]),
            divergence_slippage_mult=float(paper["divergence_slippage_mult"]),
            wake_delay_seconds=float(paper["wake_delay_seconds"]),
            clock_skew_max_seconds=float(paper["clock_skew_max_seconds"]),
            missed_wake_max=int(paper["missed_wake_max"]),
            missed_wake_window_days=int(paper["missed_wake_window_days"]),
        )


def feature_frame(
    candles: pd.DataFrame,
    feature_start: pd.Timestamp,
    candidate: Candidate,
    outages: Sequence[store.Gap],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Bars and features from a FIXED start. The slow EMA keeps a trace of its first value,
    so features must not depend on how much older history the store happens to hold
    (e.g. after a manual backfill that prepends data): otherwise every decision bar would
    look "revised" to the divergence monitor and excuse every mismatch."""
    bars = resample(
        candles[candles["timestamp"] >= feature_start], candidate.timeframe, HOUR, outages
    )
    return bars, features(bars, pd.Timedelta(candidate.timeframe), candidate.params)


def _ms(ts: pd.Timestamp) -> int:
    return int(ts.timestamp() * 1000)


def _float(value: Any) -> float:
    return float(value)


def _utcnow() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


class PaperLoop:
    def __init__(
        self,
        settings: Settings,
        exchange: Any,
        journal: Journal,
        broker: Broker,
        alerter: Alerter | None = None,
        clock: Callable[[], pd.Timestamp] = _utcnow,
    ) -> None:
        self.s, self.ex, self.j, self.broker = settings, exchange, journal, broker
        self.alerter = alerter or Alerter()
        self.clock = clock
        self.step_td = pd.Timedelta(settings.candidate.timeframe)
        self.strategy = settings.candidate.strategy(settings.sizing)
        pinned = self.j.get_float("initial_equity")
        if pinned is None:  # first start: pin it; the replay and P&L are relative to it
            with self.j.transaction():
                self.j.set("initial_equity", settings.initial_equity)
                if self.j.get("cash") is None:
                    self.j.set("cash", settings.initial_equity)
        elif pinned != settings.initial_equity:
            raise EquityMismatch(
                f"this journal started with initial equity {pinned:.2f} but the config / "
                f"TRADER_PAPER_EQUITY now says {settings.initial_equity:.2f}; restore the "
                "original value, or start a new paper run with a new journal"
            )
        self.restore()

    # --- state -------------------------------------------------------------------------

    def restore(self) -> None:
        """Rebuild the broker's resting stops from the journal (crash-safe restart)."""
        for symbol in list(self.broker.resting_stops()):
            self.broker.cancel_stop(symbol)
        for p in self.j.positions().values():
            self.broker.set_stop(p.symbol, p.qty, p.stop_price)

    @property
    def initial_equity(self) -> float:
        value = self.j.get_float("initial_equity")
        assert value is not None
        return value

    @property
    def cash(self) -> float:
        value = self.j.get_float("cash")
        assert value is not None
        return value

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return retry_call(fn, *args, delays=self.s.retry_delays, **kwargs)

    def _tripped(self) -> bool:
        return ks.load(self.s.ks_path).tripped  # re-read from disk before every order

    def _save_ks(self, before: ks.State, after: ks.State, now: pd.Timestamp) -> None:
        if after != before:
            ks.save(self.s.ks_path, after)
        if after.tripped and not before.tripped:
            ks.record(self.s.ks_path, "trip", after.reason, now)
            self.j.add_event(now, "killswitch", after.reason)
            log.error("kill switch tripped: %s", after.reason)
            self.alerter.alert("kill switch tripped", after.reason)

    def _once_a_day(self, key: str, now: pd.Timestamp) -> bool:
        """True the first time `key` is asked about on each UTC day."""
        day = now.floor("D")
        if self.j.get_time(key) == day:
            return False
        self.j.set(key, day)
        return True

    # --- market data -------------------------------------------------------------------

    def _outages(self, symbol: str) -> list[store.Gap]:
        documented = list(self.s.outages.get(symbol, ()))
        runtime = [store.Gap(a, b, int((b - a) / HOUR) + 1) for a, b in self.j.outages(symbol)]
        return documented + runtime

    def _new_outages(
        self, symbol: str, candles: pd.DataFrame, now: pd.Timestamp
    ) -> tuple[pd.DataFrame, list[store.Gap]]:
        """Missing hours in the history window that are not documented outages are
        refetched, on every wake, so a candle published late still arrives. Runtime
        outages whose hours have since appeared are removed from the journal; whatever is
        still missing and not yet recorded is a new runtime outage."""
        window = now - pd.Timedelta(hours=self.s.history_hours)
        documented = {(g.start, g.end) for g in self.s.outages.get(symbol, ())}
        for g in store.find_gaps(candles[candles["timestamp"] >= window], HOUR):
            if (g.start, g.end) in documented:
                continue
            refetched = fetch_ohlcv(self.ex, symbol, "1h", g.start, g.end + HOUR)
            if not refetched.empty:
                candles = store.merge(candles, refetched)
                store.save(candles, store.candle_path(self.s.data_dir, symbol, "1h"), HOUR)
        present = set(candles["timestamp"])
        for a, b in self.j.outages(symbol):
            if any(t in present for t in pd.date_range(a, b, freq=HOUR)):
                self.j.delete_outage(symbol, a)
                message = f"{symbol}: candles for runtime outage {a} .. {b} arrived; removed"
                self.j.add_event(now, "outage_resolved", message)
                log.warning(message)
        recorded = set(self.j.outages(symbol))
        remaining = store.find_gaps(candles[candles["timestamp"] >= window], HOUR)
        return candles, [g for g in remaining if (g.start, g.end) not in documented | recorded]

    def _market(
        self, now: pd.Timestamp
    ) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, list[store.Gap]]]:
        start = (now - pd.Timedelta(hours=self.s.history_hours)).floor(HOUR)
        bars, feats, new = {}, {}, {}
        for s in self.s.symbols:
            candles = backfill(self.ex, s, "1h", start, self.s.data_dir, now)
            candles, new[s] = self._new_outages(s, candles, now)
            key = f"feature_start:{s}"
            feature_start = self.j.get_time(key)
            if feature_start is None:  # pinned at first use: the store's first candle
                feature_start = pd.Timestamp(candles["timestamp"].iloc[0])
                self.j.set(key, feature_start)
            bars[s], feats[s] = feature_frame(
                candles, feature_start, self.s.candidate, self._outages(s) + new[s]
            )
        return bars, feats, new

    def _ref_price(self, symbol: str, t: pd.Timestamp, now: pd.Timestamp) -> float:
        """Execution reference: the open of the 1h candle starting at `t` (the new bar's
        open on an on-time wake). If the candle feed is stale, fall back to the order-book
        mid so exits can still execute; entries are blocked by the freshness check."""
        rows = self._call(self.ex.fetch_ohlcv, symbol, "1h", since=_ms(t), limit=1)
        if rows and int(rows[0][0]) == _ms(t):
            return float(rows[0][1])
        mid = self.broker.mid(symbol)
        self.j.add_event(now, "stale_feed", f"{symbol}: no 1h candle at {t}; using book mid")
        log.warning("%s: no 1h candle at %s; reference price from order book mid", symbol, t)
        return mid

    def _clock_skew(self) -> float | None:
        """Seconds between our clock and the exchange's; None if it could not be checked."""
        try:
            before = self.clock()
            server = int(self._call(self.ex.fetch_time))
            after = self.clock()
        except ccxt.BaseError as e:
            log.warning("clock check failed: %s", e)
            return None
        local = before + (after - before) / 2
        return abs(_ms(local) - server) / 1000

    def _parity(
        self, bars: dict[str, pd.DataFrame], now: pd.Timestamp, n: int
    ) -> list[dict[str, Any]]:
        """Our last `n` closed 4h bars vs the exchange's native 4h klines."""
        mismatches: list[dict[str, Any]] = []
        for s in self.s.symbols:
            ours = bars[s].set_index("timestamp")
            since = ours.index[-1] - (n - 1) * self.step_td
            rows = self._call(
                self.ex.fetch_ohlcv, s, self.s.candidate.timeframe, since=_ms(since), limit=n
            )
            closed = [
                r for r in rows if pd.Timestamp(r[0], unit="ms", tz="UTC") + self.step_td <= now
            ]
            native = store.to_frame(closed).set_index("timestamp")
            for t in native.index.union(ours.index[ours.index >= since]):
                for field in ("open", "high", "low", "close", "volume"):
                    a = _float(ours.at[t, field]) if t in ours.index else None
                    b = _float(native.at[t, field]) if t in native.index else None
                    tol = 1e-6 if field == "volume" else 1e-9
                    if a is None or b is None or abs(a - b) > tol * max(abs(a), abs(b), 1.0):
                        mismatches.append(
                            {"symbol": s, "bar_open": t, "field": field, "ours": a, "native": b}
                        )
        return mismatches

    def _newest_bar_off(self, bars: dict[str, pd.DataFrame], now: pd.Timestamp) -> list[str]:
        """Symbols whose newest bar differs from the native kline beyond tolerance. Never
        raises; a failed check blocks nothing (the full sweep after trading logs it)."""
        try:
            off = self._parity(bars, now, 1)
        except Exception as e:
            log.warning("newest-bar parity check failed: %s", e)
            return []
        return sorted(
            {m["symbol"] for m in off if m["ours"] is not None and m["native"] is not None}
        )

    # --- one wake ----------------------------------------------------------------------

    def step(self, now: pd.Timestamp) -> None:
        """One wake; alerts raised during it are sent afterwards, outside any transaction."""
        try:
            self._step(now)
        finally:
            self.alerter.flush()

    def _step(self, now: pd.Timestamp) -> None:
        """One wake. API/data errors are retried, recorded and counted once per slot; the
        scheduler retries the slot a minute later. Other errors propagate (crashes)."""
        self.j.set("heartbeat", now)
        try:
            self._wake(now)
        except StaleFeedRetry as e:  # nothing recorded; the scheduler retries next minute
            self.restore()
            log.info("wake at %s: %s; retrying", now, e)
            return
        except (ccxt.BaseError, MarketDataError) as e:
            self.restore()
            slot = now.floor(self.step_td)
            if self.j.get_time("failed_slot") != slot:
                state = ks.load(self.s.ks_path)
                self._save_ks(state, ks.on_api_error(state, now, self.s.ks_limits), now)
                self.j.set("failed_slot", slot)
            self.j.add_event(now, "api_error", f"{type(e).__name__}: {e}")
            log.warning("wake at %s failed: %s", now, e)
            return
        except BaseException:
            self.restore()
            raise
        state = ks.load(self.s.ks_path)
        self._save_ks(state, ks.on_api_success(state), now)

    def _wake(self, now: pd.Timestamp) -> None:
        step = self.step_td
        bars, feats, new_outages = self._market(now)
        last_open = min(b["timestamp"].iloc[-1] for b in bars.values())
        last_close = last_open + step
        expected = now.floor(step)
        if last_close < expected and now - expected < STALE_GRACE:
            raise StaleFeedRetry(f"newest bar closes {last_close}, expected {expected}")
        ref_time = now.floor(HOUR)  # the current 1h candle; = new bar's open when on time
        opens = {s: self._ref_price(s, ref_time, now) for s in self.s.symbols}
        stop_until = min(now.floor("1min"), last_close)
        since = self.j.get_time("stops_checked") or stop_until
        stop_fills = self.broker.poll_stop_fills(since, stop_until)
        skew = self._clock_skew()
        newest_off = self._newest_bar_off(bars, now)
        runtime = {
            s: [(a, b) for a, b in self.j.outages(s)] + [(g.start, g.end) for g in new_outages[s]]
            for s in self.s.symbols
        }
        outage_in_bar = any(
            a < last_close and b >= last_open for spans in runtime.values() for a, b in spans
        )

        with self.j.transaction():
            self.j.set("stops_checked", max(since, stop_until))
            for f in stop_fills:
                self._close_position(f, signal_time=None)
            for s, gaps in new_outages.items():
                for g in gaps:
                    if self.j.add_outage(s, g.start, g.end, now):
                        message = f"{s}: no 1h candles {g.start} .. {g.end} (treated as closed)"
                        self.j.add_event(now, "outage", message)
                        self.alerter.alert("exchange outage detected", message)

            closes = {
                s: _float(bars[s].set_index("timestamp").at[last_open, "close"])
                for s in self.s.symbols
            }
            held = {s: p.qty * closes[s] for s, p in self.j.positions().items()}
            equity = self.cash + sum(held.values())
            self.j.add_equity(last_close, equity, self.cash, sum(held.values()) / equity)
            state = ks.load(self.s.ks_path)
            self._save_ks(state, ks.on_equity(state, equity, now, self.s.ks_limits), now)
            self._record_slots(now, last_close, outage_in_bar)

            processed = self.j.get_time("last_bar_close")
            if processed is None or processed < last_close:
                if self.j.wake_status(last_close) == "stale":  # its bar finally arrived
                    self.j.set_wake_status(last_close, "late", now)
                    log.warning("wake slot %s: bar arrived late; deciding now", last_close)
                block = self._entry_block(now, skew, newest_off, outage_in_bar)
                self._decide_and_execute(now, last_close, feats, equity, opens, ref_time, block)
                self.j.set("last_bar_close", last_close)
                if self.j.get("paper_start") is None:
                    self.j.set("paper_start", last_close)

            if self._tripped() and self.s.flatten_on_trip:
                self._flatten(now, opens)
            self._overcap(now, opens)

        self._after_trading(now, bars, feats, last_close)

    def _record_slots(self, now: pd.Timestamp, last_close: pd.Timestamp, outage: bool) -> None:
        """Journal every expected wake slot since the last one recorded."""
        step = self.step_td
        expected = now.floor(step)
        previous = self.j.last_slot()
        on_time = (
            last_close == expected
            and (now - last_close).total_seconds() <= self.s.limits.stale_data_seconds
        )
        t = last_close if previous is None else previous + step
        while t <= max(expected, last_close):
            if t < last_close:
                status = "missed"
            elif t == last_close:
                status = "outage" if outage else ("processed" if on_time else "late")
            else:
                status = "stale"  # the candle feed had not produced this bar
            self.j.record_wake(t, status, now)
            if status != "processed":
                log.warning("wake slot %s: %s", t, status)
            t += step

    def _entry_block(
        self, now: pd.Timestamp, skew: float | None, newest_off: list[str], outage_in_bar: bool
    ) -> str | None:
        """Operational reasons to skip entries this wake (exits are never blocked)."""
        if skew is None:
            return "clock unverified (exchange time unavailable)"
        if skew > self.s.clock_skew_max_seconds:
            if self._once_a_day("alert_clock_day", now):
                self.alerter.alert("clock skew", f"local clock off by {skew:.1f}s")
            return f"clock skew {skew:.1f}s > {self.s.clock_skew_max_seconds}s"
        if newest_off:
            return f"parity: newest bar differs from native klines for {', '.join(newest_off)}"
        if outage_in_bar:
            return "outage in decision bar"
        window = now - pd.Timedelta(days=self.s.missed_wake_window_days)
        missed = self.j.missed_wakes(window)
        if missed > self.s.missed_wake_max:
            if self._once_a_day("alert_missed_day", now):
                self.alerter.alert(
                    "missed wakes",
                    f"{missed} missed wakes in "
                    f"{self.s.missed_wake_window_days} days; entries blocked",
                )
            return f"missed wakes: {missed} in {self.s.missed_wake_window_days} days"
        return None

    def _decide_and_execute(
        self,
        now: pd.Timestamp,
        last_close: pd.Timestamp,
        feats: dict[str, pd.DataFrame],
        equity: float,
        opens: dict[str, float],
        ref_time: pd.Timestamp,
        block: str | None,
    ) -> None:
        intents: dict[str, tuple[Intent, int]] = {}
        for s in self.s.symbols:
            f = feats[s]
            if last_close not in f.index or not bool(f.at[last_close, "ready"]):
                self.j.add_decision(now, last_close, s, None, "skip", 0.0, None, "not ready")
                continue
            values = {name: _float(f.at[last_close, name]) for name in FEATURES}
            snap = Snapshot(time=last_close, **values)
            row = self.j.positions().get(s)
            position = None if row is None else Position(row.qty, row.entry_price, row.stop_price)
            intent = self.strategy.decide(snap, position, equity)
            self.j.add_decision(
                now, last_close, s, values, intent.action.value, intent.size, intent.stop_price, ""
            )
            decision_id = self.j.last_id()
            if intent.action is Action.HOLD:
                if row is not None and intent.stop_price is not None:
                    self.j.upsert_position(replace(row, stop_price=intent.stop_price))
                    self.broker.set_stop(s, row.qty, intent.stop_price)  # replace resting stop
            else:
                intents[s] = (intent, decision_id)

        day = ref_time.floor("D")
        if self.j.get_time("day") != day:
            held = sum(p.qty * opens[s] for s, p in self.j.positions().items())
            self.j.set("day", day)
            self.j.set("day_start_equity", self.cash + held)

        for s in sorted(intents, key=lambda s: intents[s][0].action is not Action.EXIT):
            intent, decision_id = intents[s]
            positions = self.j.positions()
            if intent.action is Action.EXIT and s in positions:
                fill = self.broker.market(s, "sell", positions[s].qty, opens[s], ref_time, "signal")
                self._close_position(fill, signal_time=last_close)
                self.broker.cancel_stop(s)  # only after the sell has filled
            elif intent.action is Action.ENTER and s not in positions:
                risk = self._enter(s, intent, now, last_close, opens, ref_time, block)
                self.j.set_risk(decision_id, risk)

    def _enter(
        self,
        s: str,
        intent: Intent,
        now: pd.Timestamp,
        last_close: pd.Timestamp,
        opens: dict[str, float],
        ref_time: pd.Timestamp,
        block: str | None,
    ) -> str:
        if self._tripped():
            return "rejected: kill switch tripped"
        if block is not None:
            log.info("%s entry skipped: %s", s, block)
            return f"rejected: {block}"
        positions = self.j.positions()
        held = {s2: p.qty * opens[s2] for s2, p in positions.items()}
        day_start = self.j.get_float("day_start_equity")
        assert day_start is not None
        account = Account(self.cash + sum(held.values()), held, day_start)
        price = self.s.costs.buy_price(opens[s])
        headroom = min(
            entry_headroom(account, s, self.s.limits), self.cash / (1 + self.s.costs.fee)
        )
        qty = min(intent.size, headroom / price)
        if qty <= 0 or intent.stop_price is None:
            return "dropped: no headroom"
        qty, why = self.broker.conform(s, qty, opens[s])
        if why is not None:
            log.info("%s entry rejected: %s", s, why)
            return f"rejected: {why}"
        recent = list(self.j.frame("fills")["time"])
        decision = pre_trade_check(
            EntryOrder(s, qty * price),
            account,
            self.s.limits,
            recent,
            last_close,
            now,
            self.step_td,
        )
        if isinstance(decision, Rejected):
            log.info("%s entry rejected: %s", s, decision.reason)
            return f"rejected: {decision.reason}"
        fill = self.broker.market(s, "buy", qty, opens[s], ref_time, "entry")
        cost = fill.qty * fill.price + fill.fee
        if cost > self.cash:  # the book walked above the modeled price: buy what cash allows
            qty, why = self.broker.conform(
                s, self.cash / (fill.price * (1 + self.s.costs.fee)) * (1 - 1e-12), fill.price
            )
            if why is not None:
                return f"rejected: {why}"
            fill = replace(fill, qty=qty, fee=self.s.costs.fee_on(qty * fill.price))
            cost = fill.qty * fill.price + fill.fee
        self.j.set("cash", self.cash - cost)
        self.j.add_fill(fill, signal_time=last_close)
        self.j.upsert_position(
            PositionRow(s, fill.qty, fill.price, fill.time, cost, intent.stop_price)
        )
        self.broker.set_stop(s, fill.qty, intent.stop_price)
        if fill.qty < intent.size * (1 - 1e-9):  # not lot-size rounding noise
            return f"approved; clipped from {intent.size:.10g} to {fill.qty:.10g}"
        return "approved"

    def _close_position(self, fill: Fill, signal_time: pd.Timestamp | None) -> None:
        row = self.j.positions()[fill.symbol]
        proceeds = fill.qty * fill.price - fill.fee
        self.j.set("cash", self.cash + proceeds)
        self.j.add_fill(fill, signal_time)
        self.j.add_trade(row, fill, proceeds - row.entry_cost)
        self.j.delete_position(fill.symbol)

    def _flatten(self, now: pd.Timestamp, refs: dict[str, float] | None) -> None:
        """Market-sell every position (kill switch). The stop is cancelled only after its
        sell has filled. `refs` are reference prices; None means the current book mid."""
        for s, p in sorted(self.j.positions().items()):
            ref = refs[s] if refs is not None else self.broker.mid(s)
            fill = self.broker.market(s, "sell", p.qty, ref, now, "killswitch")
            self._close_position(fill, signal_time=None)
            self.broker.cancel_stop(s)
            log.warning("kill switch flatten: sold %s %.8g", s, p.qty)

    def _overcap(self, now: pd.Timestamp, refs: dict[str, float]) -> None:
        """Warn when a position crosses the position cap, then at most once a day."""
        positions = self.j.positions()
        values = {s: p.qty * refs[s] for s, p in positions.items()}
        day_start = self.j.get_float("day_start_equity") or self.cash
        account = Account(self.cash + sum(values.values()), values, day_start)
        over = set(overcap_symbols(account, self.s.limits, log_warnings=False))
        for s in self.s.symbols:
            key = f"overcap_day:{s}"
            if s not in over:
                self.j.set(key, "")
            elif self.j.get(key) != now.floor("D").isoformat():
                self.j.set(key, now.floor("D"))
                log.warning(
                    "%s is %.2f%% of equity, over the %s%% cap; no auto-trim, adding "
                    "to it is blocked",
                    s,
                    100 * values[s] / account.equity,
                    self.s.limits.max_position_pct,
                )

    # --- after trading: never blocks or undoes it ---------------------------------------

    def _guarded(self, now: pd.Timestamp, what: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:
            self.restore()
            log.exception("%s failed", what)
            self.j.add_event(now, f"{what}_error", f"{type(e).__name__}: {e}")

    def _after_trading(
        self,
        now: pd.Timestamp,
        bars: dict[str, pd.DataFrame],
        feats: dict[str, pd.DataFrame],
        last_close: pd.Timestamp,
    ) -> None:
        self._guarded(now, "parity", lambda: self._parity_sweep(bars, now))
        if self.j.get("paper_start") and self.j.get_time("divergence_day") != now.floor("D"):
            self._guarded(now, "divergence", lambda: self._divergence(now, bars, feats, last_close))
        if self.j.get_time("summary_day") != now.floor("D"):
            self._guarded(now, "summary", lambda: self._daily_summary(now))
        self.alerter.ping()

    def _parity_sweep(self, bars: dict[str, pd.DataFrame], now: pd.Timestamp) -> None:
        mismatches = self._parity(bars, now, self.s.parity_bars)
        with self.j.transaction():
            for m in mismatches:
                seen = self.j.add_parity(
                    now, m["symbol"], m["bar_open"], m["field"], m["ours"], m["native"]
                )
                text = (
                    f"{m['symbol']} {m['bar_open']} {m['field']}: ours {m['ours']} vs "
                    f"native {m['native']}"
                )
                if seen == 1:
                    log.warning("bar parity mismatch: %s", text)
                elif seen == 2:  # still there a wake later: escalate once
                    log.error("persistent bar parity mismatch: %s", text)
                    self.alerter.alert("persistent bar parity mismatch", text)

    def _divergence(
        self,
        now: pd.Timestamp,
        bars: dict[str, pd.DataFrame],
        feats: dict[str, pd.DataFrame],
        last_close: pd.Timestamp,
    ) -> None:
        paper_start = self.j.get_time("paper_start")
        assert paper_start is not None
        result = divergence.replay(
            bars,
            feats,
            self.strategy,
            self.s.costs,
            self.s.limits,
            self.initial_equity,
            paper_start,
            last_close,
            self.step_td,
        )
        slots = self.j.wake_slots()
        decisions = self.j.frame("decisions")
        unclean = set(slots.loc[slots["status"] != "processed", "slot"])
        unclean |= divergence.revised_bars(decisions, feats)
        declined = decisions["risk"].fillna("").str.startswith(OPERATIONAL_REJECTIONS)
        unclean |= set(decisions.loc[declined, "bar_close"])
        recent = set(
            decisions.loc[decisions["bar_close"] >= now - pd.Timedelta(days=30), "bar_close"]
        )
        excused = len(recent & unclean) / len(recent) if recent else 0.0
        unexplained, explained = divergence.compare(
            self.j.frame("trades"),
            self.j.positions(),
            result,
            last_close,
            self.step_td,
            self.j.frame("fills"),
            unclean,
        )
        report = divergence.slippage_check(
            self.j.frame("fills"),
            self.s.costs.slippage,
            self.s.divergence_min_fills,
            self.s.divergence_slippage_mult,
        )
        with self.j.transaction():
            self.j.set("divergence_day", now.floor("D"))
            for m in explained:
                self.j.add_event(now, "divergence_explained", m)
            if explained:
                self.alerter.alert(
                    "explained divergence",
                    f"{len(explained)} mismatches "
                    "explained by missed/late wakes, outages, revisions or "
                    f"declined entries; first: {explained[0]}",
                )
            if unexplained:
                for m in unexplained:
                    self.j.add_event(now, "divergence", m)
                state = ks.load(self.s.ks_path)
                message = f"divergence from backtest replay: {unexplained[0]}"
                self._save_ks(state, ks.trip(state, message, now), now)
                if self.s.flatten_on_trip:
                    self._flatten(now, None)  # flatten now, not at the next bar
            if excused > 0.10:  # the monitor would excuse too much to be meaningful
                message = (
                    f"{excused:.0%} of decision bars in the last 30 days are unclean "
                    "(missed/late/stale wakes, outages, revisions, declined entries); "
                    "divergence checks are mostly excused"
                )
                self.j.add_event(now, "divergence_overexcused", message)
                self.alerter.alert("divergence monitor over-excusing", message)
            text = report.describe(self.s.costs.slippage)
            self.j.add_event(now, "slippage_warning" if report.warn else "slippage", text)
            if report.warn:
                log.warning(
                    "fill slippage above %sx model: %s", self.s.divergence_slippage_mult, text
                )
                self.alerter.alert("fill slippage above model", text)

    def _daily_summary(self, now: pd.Timestamp) -> None:
        trades = self.j.frame("trades")
        recent = trades[trades["exit_time"] > now - pd.Timedelta(days=1)]
        equity = self.j.frame("equity", order="time").tail(1)
        state = ks.load(self.s.ks_path)
        missed = self.j.missed_wakes(now - pd.Timedelta(days=self.s.missed_wake_window_days))
        positions = (
            ", ".join(
                f"{p.symbol} {p.qty:.6g} stop {p.stop_price:.2f}"
                for p in self.j.positions().values()
            )
            or "flat"
        )
        value = f"{equity['equity'].iloc[0]:.2f}" if not equity.empty else "n/a"
        text = (
            f"equity {value}, cash {self.cash:.2f}; positions: {positions}; trades closed "
            f"in 24h: {len(recent)} (P&L {recent['pnl'].sum():.2f}); missed wakes in "
            f"{self.s.missed_wake_window_days}d: {missed}; kill switch "
            f"{'TRIPPED: ' + state.reason if state.tripped else 'armed'}"
        )
        with self.j.transaction():
            self.j.set("summary_day", now.floor("D"))
            self.j.add_event(now, "summary", text)
        self.alerter.alert("daily summary", text)

    # --- minute ticks and scheduling ---------------------------------------------------

    def tick(self, now: pd.Timestamp) -> None:
        try:
            self._tick(now)
        finally:
            self.alerter.flush()

    def _tick(self, now: pd.Timestamp) -> None:
        """Between wakes: poll resting stops (never past the newest decided bar, so fills
        match the replay) and flatten promptly if the kill switch tripped. Pings the
        dead-man URL every minute so a dead machine is noticed within minutes."""
        self.j.set("heartbeat", now)
        self.alerter.ping()
        decided = self.j.get_time("last_bar_close")
        if decided is None:
            return
        try:
            until = min(now.floor("1min"), decided + self.step_td)
            since = self.j.get_time("stops_checked") or until
            fills = self.broker.poll_stop_fills(since, until)
            flatten = self.s.flatten_on_trip and self._tripped() and bool(self.j.positions())
            with self.j.transaction():
                self.j.set("stops_checked", max(since, until))
                for f in fills:
                    self._close_position(f, signal_time=None)
                if flatten:
                    self._flatten(now, None)
        except Exception as e:  # ticks never count toward the kill switch; the wake does
            self.restore()
            log.warning("tick at %s failed: %s", now, e)
            self.j.add_event(now, "tick_error", f"{type(e).__name__}: {e}")

    def run_once(self, now: pd.Timestamp) -> pd.Timestamp:
        """One scheduler iteration: the wake if its slot is due, otherwise a tick. Returns
        when to run next."""
        slot = now.floor(self.step_td)
        due = slot + pd.Timedelta(seconds=self.s.wake_delay_seconds)
        last = self.j.last_slot()
        decided = self.j.get_time("last_bar_close")
        attempted = self.j.get_time("wake_attempt")
        retry_stale = (  # the slot went stale; keep looking for its bar until the next slot
            last == slot
            and self.j.wake_status(slot) == "stale"
            and (decided is None or decided < slot)
            and (attempted is None or now - attempted >= STALE_RETRY)
        )
        if now >= due and (last is None or last < slot or retry_stale):
            self.j.set("wake_attempt", now)
            try:
                self.step(now)
                self.j.set("consecutive_crashes", 0)
            except Exception as e:
                crashes = int(self.j.get("consecutive_crashes") or 0) + 1
                self.j.set("consecutive_crashes", crashes)
                log.exception("wake at %s crashed (%d in a row)", now, crashes)
                self.j.add_event(now, "crash", f"{type(e).__name__}: {e}")
                if crashes >= MAX_CRASHES:
                    state = ks.load(self.s.ks_path)
                    self._save_ks(state, ks.trip(state, f"{crashes} consecutive crashes", now), now)
                self.alerter.flush()
            recorded = self.j.last_slot()
            if recorded is None or recorded < slot:
                # The wake is still retrying (stale feed, API outage, crash): keep stop
                # polling, prompt kill-switch flatten and the dead-man ping going.
                self.tick(now)
        else:
            self.tick(now)
        next_minute = now.floor("1min") + MINUTE + pd.Timedelta(seconds=2)
        next_due = due if now < due else due + self.step_td
        return min(next_minute, next_due)

    def run_forever(self) -> None:
        while True:
            wake_at = self.run_once(self.clock())
            remaining = (wake_at - self.clock()).total_seconds()
            if remaining > 0:
                time.sleep(remaining)


# --- single instance and start-up checks -----------------------------------------------


@contextmanager
def single_instance(path: Path) -> Iterator[IO[str]]:
    """OS-level lock released automatically if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+")
    try:
        f.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        f.close()
        raise RuntimeError(f"another trader instance holds {path}") from e
    try:
        yield f
    finally:
        f.close()


def git_state(cwd: Path = Path(".")) -> tuple[str, bool] | None:
    """(commit, dirty) for tracked files, or None if git is unavailable."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return commit, bool(status.strip())


# --- CLIs ------------------------------------------------------------------------------


def _load(config: Path) -> tuple[dict[str, Any], Settings]:
    cfg = yaml.safe_load(config.read_text())
    return cfg, Settings.from_config(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper-trading loop for strategy v1.")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--once", action="store_true", help="run one wake now and exit")
    args = parser.parse_args(argv)
    cfg, settings = _load(args.config)
    if cfg["mode"] != "paper":
        print("only mode: paper is supported")
        return 2
    state = git_state()
    if state is None or state[1]:
        print(
            "refusing to start: not a git checkout, or tracked files have uncommitted "
            "changes (every paper result must map to a commit)"
        )
        return 2
    paper = cfg["paper"]
    setup_logging(Path(paper["log_dir"]))
    config_hash = hashlib.sha256(args.config.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    with single_instance(Path(paper["lock"])):
        exchange = getattr(ccxt, cfg["exchange"])({"enableRateLimit": True})
        journal = Journal(Path(paper["db"]))
        broker = PaperBroker(exchange, settings.costs, retry_delays=settings.retry_delays)
        try:
            loop = PaperLoop(settings, exchange, journal, broker, Alerter.from_env())
        except EquityMismatch as e:
            print(f"refusing to start: {e}")
            return 2
        log.info(
            "paper loop starting (%s): commit %s, config sha256 %s, equity %.2f",
            "once" if args.once else "forever",
            state[0],
            config_hash,
            loop.initial_equity,
        )
        journal.add_event(_utcnow(), "start", f"commit {state[0]} config {config_hash}")
        if args.once:
            loop.step(_utcnow())
        else:
            loop.run_forever()
    return 0


def status_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper loop status.")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument(
        "--check-heartbeat",
        action="store_true",
        help="exit 1 (and alert) if the heartbeat is older than 10 minutes",
    )
    args = parser.parse_args(argv)
    cfg, settings = _load(args.config)
    db = Path(cfg["paper"]["db"])
    if not db.exists():
        print(f"no journal at {db}")
        return 1
    j = Journal(db)
    now = _utcnow()
    heartbeat = j.get_time("heartbeat")
    age = None if heartbeat is None else now - heartbeat
    stale = age is None or age > pd.Timedelta(minutes=10)  # ticks beat every minute
    state = ks.load(settings.ks_path)
    print(f"heartbeat: {heartbeat} (age {age}){'  STALE' if stale else ''}")
    print(f"kill switch: {'TRIPPED' if state.tripped else 'armed'} ({state.reason})")
    print(
        f"cash: {j.get('cash')}  last bar processed: {j.get('last_bar_close')}  "
        f"paper start: {j.get('paper_start')}"
    )
    missed = j.missed_wakes(now - pd.Timedelta(days=settings.missed_wake_window_days))
    print(f"missed wakes in {settings.missed_wake_window_days} days: {missed}")
    for p in j.positions().values():
        print(
            f"position {p.symbol}: qty {p.qty:.8g} @ {p.entry_price:.2f} since {p.entry_time}, "
            f"stop {p.stop_price:.2f}"
        )
    equity = j.frame("equity", order="time").tail(1)
    if not equity.empty:
        print(f"equity @ {equity['time'].iloc[0]}: {equity['equity'].iloc[0]:.2f}")
    for d in j.frame("decisions").tail(6).itertuples():
        print(f"decision {d.bar_close} {d.symbol}: {d.action} {d.risk}")
    for e in j.frame("events").tail(5).itertuples():
        print(f"event {e.time} {e.kind}: {e.message}")
    j.close()
    if args.check_heartbeat and stale:
        alerter = Alerter.from_env()
        alerter.alert("stale heartbeat", f"last heartbeat {heartbeat} (age {age})")
        alerter.flush()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
