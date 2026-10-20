"""Paper-trading loop for strategy v1 (SPEC section 5), paper only.

One wake per 4h bar close (UTC), about `wake_delay_seconds` after it. A wake mirrors the
backtest engine's per-bar order, so a replay reproduces the engine trade for trade:

  1. refresh 1h candles (public endpoints), build 4h bars with the backtest's resample()
     and features(); check them against the exchange's native 4h klines;
  2. resting stops: fills on 1m candles since the last check;
  3. mark equity at the latest closed bar; kill switch on_equity;
  4. decide on the latest closed bar (once per bar). HOLD ratchets the stop; exits always
     execute, even on a late wake; entries need pre_trade_check, including freshness, so
     a late or stale wake skips them rather than chasing;
  5. execute at the current 1h candle's open (= the new bar's open on an on-time wake):
     exits first, then entries; a stop is cancelled only after its position's sell fills;
  6. kill switch tripped: entries blocked; with flatten_on_trip, market-sell everything;
  7. once a UTC day: divergence check against a backtest replay.

All state lives in the SQLite journal; each wake commits atomically.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
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
from trader.data.fetch import backfill
from trader.data.resample import resample
from trader.exec import divergence
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


class MarketDataError(Exception):
    """Exchange data missing or inconsistent; handled like an API error."""


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
    outages: Mapping[str, Sequence[store.Gap]]
    history_hours: int
    parity_bars: int
    divergence_min_fills: int
    divergence_slippage_mult: float

    @classmethod
    def from_config(cls, cfg: dict[str, Any], root: Path = Path(".")) -> Settings:
        paper, risk = cfg["paper"], cfg["risk"]
        protocol = yaml.safe_load((root / paper["strategy_protocol"]).read_text())
        name = paper["strategy_candidate"]
        candidate = Candidate.from_protocol(name, protocol["candidates"][name], protocol)
        gaps = root / paper["known_gaps"]
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
            initial_equity=float(paper["initial_equity"]),
            flatten_on_trip=bool(risk["flatten_on_trip"]),
            data_dir=root / paper["data_dir"],
            outages={s: store.load_known_gaps(gaps, s, "1h") for s in cfg["pairs"]},
            history_hours=int(paper["history_hours"]),
            parity_bars=int(paper["parity_bars"]),
            divergence_min_fills=int(paper["divergence_min_fills"]),
            divergence_slippage_mult=float(paper["divergence_slippage_mult"]),
        )


def _ms(ts: pd.Timestamp) -> int:
    return int(ts.timestamp() * 1000)


def _float(value: Any) -> float:
    return float(value)


class PaperLoop:
    def __init__(self, settings: Settings, exchange: Any, journal: Journal, broker: Broker):
        self.s, self.ex, self.j, self.broker = settings, exchange, journal, broker
        self.step_td = pd.Timedelta(settings.candidate.timeframe)
        self.strategy = settings.candidate.strategy(settings.sizing)
        if self.j.get("cash") is None:
            with self.j.transaction():
                self.j.set("cash", settings.initial_equity)
        self.restore()

    # --- state -------------------------------------------------------------------------

    def restore(self) -> None:
        """Rebuild the broker's resting stops from the journal (crash-safe restart)."""
        for symbol in list(self.broker.resting_stops()):
            self.broker.cancel_stop(symbol)
        for p in self.j.positions().values():
            self.broker.set_stop(p.symbol, p.qty, p.stop_price)

    @property
    def cash(self) -> float:
        value = self.j.get_float("cash")
        assert value is not None
        return value

    def _tripped(self) -> bool:
        return ks.load(self.s.ks_path).tripped  # re-read from disk before every order

    def _save_ks(self, before: ks.State, after: ks.State, now: pd.Timestamp) -> None:
        if after != before:
            ks.save(self.s.ks_path, after)
        if after.tripped and not before.tripped:
            ks.record(self.s.ks_path, "trip", after.reason, now)
            self.j.add_event(now, "killswitch", after.reason)
            log.error("kill switch tripped: %s", after.reason)

    # --- market data -------------------------------------------------------------------

    def _bars(self, now: pd.Timestamp) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
        start = (now - pd.Timedelta(hours=self.s.history_hours)).floor(HOUR)
        bars, feats = {}, {}
        for s in self.s.symbols:
            candles = backfill(self.ex, s, "1h", start, self.s.data_dir, now)
            bars[s] = resample(candles, self.s.candidate.timeframe, HOUR, self.s.outages.get(s, ()))
            feats[s] = features(bars[s], self.step_td, self.s.candidate.params)
        return bars, feats

    def _ref_price(self, symbol: str, t: pd.Timestamp, now: pd.Timestamp) -> float:
        """Execution reference: the open of the 1h candle starting at `t` (the new bar's
        open on an on-time wake). If the candle feed is stale, fall back to the order-book
        mid so exits can still execute; entries are blocked by the freshness check."""
        rows = self.ex.fetch_ohlcv(symbol, "1h", since=_ms(t), limit=1)
        if rows and int(rows[0][0]) == _ms(t):
            return float(rows[0][1])
        book = self.ex.fetch_order_book(symbol, limit=5)
        if not book["bids"] or not book["asks"]:
            raise MarketDataError(f"no {symbol} candle at {t} and an empty order book")
        mid = (float(book["bids"][0][0]) + float(book["asks"][0][0])) / 2
        self.j.add_event(now, "stale_feed", f"{symbol}: no 1h candle at {t}; using book mid {mid}")
        log.warning("%s: no 1h candle at %s; reference price from order book mid", symbol, t)
        return mid

    def _parity(self, bars: dict[str, pd.DataFrame], now: pd.Timestamp) -> list[dict[str, Any]]:
        """Compare our last closed 4h bars with the exchange's native 4h klines."""
        mismatches: list[dict[str, Any]] = []
        n = self.s.parity_bars
        for s in self.s.symbols:
            ours = bars[s].set_index("timestamp")
            since = ours.index[-1] - (n - 1) * self.step_td
            native_rows = self.ex.fetch_ohlcv(
                s, self.s.candidate.timeframe, since=_ms(since), limit=n
            )
            native = store.to_frame(
                [
                    r
                    for r in native_rows
                    if pd.Timestamp(r[0], unit="ms", tz="UTC") + self.step_td <= now
                ]
            ).set_index("timestamp")
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

    # --- one wake ----------------------------------------------------------------------

    def step(self, now: pd.Timestamp) -> None:
        self.j.set("heartbeat", now)
        try:
            self._step(now)
        except (ccxt.BaseError, MarketDataError) as e:
            self.restore()
            state = ks.load(self.s.ks_path)
            self._save_ks(state, ks.on_api_error(state, now, self.s.ks_limits), now)
            self.j.add_event(now, "api_error", f"{type(e).__name__}: {e}")
            log.warning("wake at %s failed: %s", now, e)
            return
        except BaseException:
            self.restore()
            raise
        state = ks.load(self.s.ks_path)
        self._save_ks(state, ks.on_api_success(state), now)

    def _step(self, now: pd.Timestamp) -> None:
        step = self.step_td
        bars, feats = self._bars(now)
        last_open = min(b["timestamp"].iloc[-1] for b in bars.values())
        last_close = last_open + step
        until = now.floor("1min")
        since = self.j.get_time("stops_checked") or until
        stop_fills = self.broker.poll_stop_fills(since, until)
        ref_time = now.floor(HOUR)  # the current 1h candle; = new bar's open when on time
        opens = {s: self._ref_price(s, ref_time, now) for s in self.s.symbols}
        mismatches = self._parity(bars, now)

        with self.j.transaction():
            self.j.set("stops_checked", until)
            for f in stop_fills:
                self._close_position(f, signal_time=None)
            for m in mismatches:
                self.j.add_parity(
                    now, m["symbol"], m["bar_open"], m["field"], m["ours"], m["native"]
                )
            if mismatches:
                log.warning(
                    "%d bar parity mismatches vs native %s klines",
                    len(mismatches),
                    self.s.candidate.timeframe,
                )

            closes = {
                s: _float(bars[s].set_index("timestamp").at[last_open, "close"])
                for s in self.s.symbols
            }
            held = {s: p.qty * closes[s] for s, p in self.j.positions().items()}
            equity = self.cash + sum(held.values())
            self.j.add_equity(last_close, equity, self.cash, sum(held.values()) / equity)
            state = ks.load(self.s.ks_path)
            self._save_ks(state, ks.on_equity(state, equity, now, self.s.ks_limits), now)

            processed = self.j.get_time("last_bar_close")
            if processed is None or processed < last_close:
                self._decide_and_execute(now, last_close, feats, equity, opens, ref_time)
                self.j.set("last_bar_close", last_close)
                if self.j.get("paper_start") is None:
                    self.j.set("paper_start", last_close)

            if self._tripped() and self.s.flatten_on_trip:
                for s, p in sorted(self.j.positions().items()):
                    fill = self.broker.market(s, "sell", p.qty, opens[s], now, "killswitch")
                    self._close_position(fill, signal_time=None)
                    self.broker.cancel_stop(s)  # only after the sell has filled

            account = Account(
                self.cash + sum(p.qty * opens[s] for s, p in self.j.positions().items()),
                {s: p.qty * opens[s] for s, p in self.j.positions().items()},
                self.j.get_float("day_start_equity") or equity,
            )
            overcap_symbols(account, self.s.limits)

            day = now.floor("D")
            if self.j.get_time("divergence_day") != day and self.j.get("paper_start"):
                self._divergence(now, bars, feats, last_close)
                self.j.set("divergence_day", day)

    def _decide_and_execute(
        self,
        now: pd.Timestamp,
        last_close: pd.Timestamp,
        feats: dict[str, pd.DataFrame],
        equity: float,
        opens: dict[str, float],
        ref_time: pd.Timestamp,
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
                risk = self._enter(s, intent, now, last_close, opens, ref_time)
                self.j.set_risk(decision_id, risk)

    def _enter(
        self,
        s: str,
        intent: Intent,
        now: pd.Timestamp,
        last_close: pd.Timestamp,
        opens: dict[str, float],
        ref_time: pd.Timestamp,
    ) -> str:
        if self._tripped():
            return "rejected: kill switch tripped"
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
            qty = self.cash / (fill.price * (1 + self.s.costs.fee)) * (1 - 1e-12)
            fill = replace(fill, qty=qty, fee=self.s.costs.fee_on(qty * fill.price))
            cost = fill.qty * fill.price + fill.fee
        self.j.set("cash", self.cash - cost)
        self.j.add_fill(fill, signal_time=last_close)
        self.j.upsert_position(
            PositionRow(s, fill.qty, fill.price, fill.time, cost, intent.stop_price)
        )
        self.broker.set_stop(s, fill.qty, intent.stop_price)
        clipped = (
            f"; clipped from {intent.size:.10g} to {fill.qty:.10g}"
            if fill.qty < intent.size
            else ""
        )
        return f"approved{clipped}"

    def _close_position(self, fill: Fill, signal_time: pd.Timestamp | None) -> None:
        row = self.j.positions()[fill.symbol]
        proceeds = fill.qty * fill.price - fill.fee
        self.j.set("cash", self.cash + proceeds)
        self.j.add_fill(fill, signal_time)
        self.j.add_trade(row, fill, proceeds - row.entry_cost)
        self.j.delete_position(fill.symbol)

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
            self.s.initial_equity,
            paper_start,
            last_close,
            self.step_td,
        )
        mismatches = divergence.compare(
            self.j.frame("trades"),
            self.j.positions(),
            result,
            last_close,
            self.step_td,
            self.j.frame("fills"),
        )
        if mismatches:
            message = f"divergence from backtest replay: {mismatches[0]}"
            state = ks.load(self.s.ks_path)
            self._save_ks(state, ks.trip(state, message, now), now)
            for m in mismatches:
                self.j.add_event(now, "divergence", m)
        n, avg, warn = divergence.slippage_check(
            self.j.frame("fills"),
            self.s.costs.slippage,
            self.s.divergence_min_fills,
            self.s.divergence_slippage_mult,
        )
        message = (
            f"{n} market fills, average slippage {avg:.4%} vs model {self.s.costs.slippage:.4%}"
        )
        self.j.add_event(now, "slippage_warning" if warn else "slippage", message)
        if warn:
            log.warning(
                "fill slippage above %sx model: %s", self.s.divergence_slippage_mult, message
            )

    # --- scheduling --------------------------------------------------------------------

    def run_forever(self, wake_delay: float) -> None:
        while True:
            now = pd.Timestamp.now(tz="UTC")
            try:
                self.step(now)
            except Exception:  # never die silently; count it like an API error
                log.exception("wake at %s crashed", now)
                state = ks.load(self.s.ks_path)
                self._save_ks(state, ks.on_api_error(state, now, self.s.ks_limits), now)
                self.j.add_event(now, "crash", "see log")
            wake = now.floor(self.step_td) + self.step_td + pd.Timedelta(seconds=wake_delay)
            while (remaining := (wake - pd.Timestamp.now(tz="UTC")).total_seconds()) > 0:
                time.sleep(min(60.0, remaining))
                self.j.set("heartbeat", pd.Timestamp.now(tz="UTC"))


# --- single instance -------------------------------------------------------------------


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
    paper = cfg["paper"]
    setup_logging(Path(paper["log_dir"]))
    with single_instance(Path(paper["lock"])):
        exchange = getattr(ccxt, cfg["exchange"])({"enableRateLimit": True})
        journal = Journal(Path(paper["db"]))
        loop = PaperLoop(settings, exchange, journal, PaperBroker(exchange, settings.costs))
        log.info("paper loop starting (%s)", "once" if args.once else "forever")
        if args.once:
            loop.step(pd.Timestamp.now(tz="UTC"))
        else:
            loop.run_forever(float(paper["wake_delay_seconds"]))
    return 0


def status_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper loop status.")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument(
        "--check-heartbeat",
        action="store_true",
        help="exit 1 if the heartbeat is older than one bar plus 10 minutes",
    )
    args = parser.parse_args(argv)
    cfg, settings = _load(args.config)
    db = Path(cfg["paper"]["db"])
    if not db.exists():
        print(f"no journal at {db}")
        return 1
    j = Journal(db)
    now = pd.Timestamp.now(tz="UTC")
    heartbeat = j.get_time("heartbeat")
    age = None if heartbeat is None else now - heartbeat
    stale = age is None or age > pd.Timedelta(settings.candidate.timeframe) + pd.Timedelta(
        minutes=10
    )
    state = ks.load(settings.ks_path)
    print(f"heartbeat: {heartbeat} (age {age}){'  STALE' if stale else ''}")
    print(f"kill switch: {'TRIPPED' if state.tripped else 'armed'} ({state.reason})")
    print(
        f"cash: {j.get('cash')}  last bar processed: {j.get('last_bar_close')}  "
        f"paper start: {j.get('paper_start')}"
    )
    for p in j.positions().values():
        print(
            f"position {p.symbol}: qty {p.qty:.8g} @ {p.entry_price:.2f} since {p.entry_time}, "
            f"stop {p.stop_price:.2f}"
        )
    equity = j.frame("equity", order="time").tail(1)
    if not equity.empty:
        print(f"equity @ {equity['time'].iloc[0]}: {equity['equity'].iloc[0]:.2f}")
    decisions = j.frame("decisions").tail(6)
    for d in decisions.itertuples():
        print(f"decision {d.bar_close} {d.symbol}: {d.action} {d.risk}")
    events = j.frame("events").tail(5)
    for e in events.itertuples():
        print(f"event {e.time} {e.kind}: {e.message}")
    j.close()
    return 1 if args.check_heartbeat and stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
