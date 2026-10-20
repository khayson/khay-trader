from pathlib import Path

import pandas as pd
import pytest
import yaml

from trader.risk import limits
from trader.risk.limits import Account, Approved, EntryOrder, Rejected, RiskLimits

STEP = pd.Timedelta(hours=4)
CLOSE = pd.Timestamp("2025-03-10 08:00", tz="UTC")  # a 4h bar close
LIM = RiskLimits(
    max_position_pct=10,
    max_total_exposure_pct=20,
    max_daily_loss_pct=2,
    max_orders_per_hour=6,
    stale_data_seconds=180,
)
ACCOUNT = Account(equity=10_000.0, position_values={}, day_start_equity=10_000.0)
ORDER = EntryOrder("BTC/USDT", 500.0)
NOW = CLOSE + pd.Timedelta(seconds=10)
EPS = 1e-6  # far above the float slack in limits, far below any real order


def check(
    order: EntryOrder = ORDER,
    account: Account = ACCOUNT,
    recent: tuple[pd.Timestamp, ...] = (),
    last_close: pd.Timestamp = CLOSE,
    now: pd.Timestamp = NOW,
) -> Approved | Rejected:
    return limits.pre_trade_check(order, account, LIM, recent, last_close, now, STEP)


def test_from_config() -> None:
    cfg = yaml.safe_load((Path(__file__).parent.parent / "config" / "default.yaml").read_text())
    assert RiskLimits.from_config(cfg) == LIM


def test_clean_order_approved() -> None:
    assert check() == Approved()


# --- position cap ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("notional", "ok"), [(1000.0, True), (1000.0 - EPS, True), (1000.0 + EPS, False)]
)
def test_position_cap_boundary(notional: float, ok: bool) -> None:
    result = check(EntryOrder("BTC/USDT", notional))
    assert (result == Approved()) is ok
    if not ok:
        assert isinstance(result, Rejected) and "position cap" in result.reason


def test_position_cap_counts_existing_holding() -> None:
    acct = Account(10_000.0, {"BTC/USDT": 600.0}, 10_000.0)
    assert check(EntryOrder("BTC/USDT", 400.0), acct) == Approved()
    assert isinstance(check(EntryOrder("BTC/USDT", 400.0 + EPS), acct), Rejected)


def test_overgrown_position_blocks_adding_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    acct = Account(10_000.0, {"BTC/USDT": 1_200.0}, 10_000.0)  # grew past 10% after fill
    assert isinstance(check(EntryOrder("BTC/USDT", 1.0), acct), Rejected)
    assert check(EntryOrder("ETH/USDT", 500.0), acct) == Approved()  # other pair unaffected
    assert limits.overcap_symbols(acct, LIM) == ["BTC/USDT"]
    assert "BTC/USDT" in caplog.text and "no auto-trim" in caplog.text


# --- total exposure --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("notional", "ok"), [(800.0, True), (800.0 - EPS, True), (800.0 + EPS, False)]
)
def test_total_exposure_boundary(notional: float, ok: bool) -> None:
    acct = Account(10_000.0, {"ETH/USDT": 1_200.0}, 10_000.0)  # 12% held, 8% headroom
    result = check(EntryOrder("BTC/USDT", notional), acct)
    assert (result == Approved()) is ok
    if not ok:
        assert isinstance(result, Rejected) and "exposure" in result.reason


def test_entry_headroom_matches_caps() -> None:
    acct = Account(10_000.0, {"ETH/USDT": 1_200.0}, 10_000.0)
    assert limits.entry_headroom(acct, "BTC/USDT", LIM) == pytest.approx(800.0)
    assert limits.entry_headroom(ACCOUNT, "BTC/USDT", LIM) == pytest.approx(1_000.0)
    full = Account(10_000.0, {"ETH/USDT": 2_500.0}, 10_000.0)
    assert limits.entry_headroom(full, "BTC/USDT", LIM) == 0.0


# --- daily loss ------------------------------------------------------------------------------


@pytest.mark.parametrize(("equity", "ok"), [(9_800.01, True), (9_800.0, False), (9_799.99, False)])
def test_daily_loss_boundary(equity: float, ok: bool) -> None:
    acct = Account(equity, {}, day_start_equity=10_000.0)  # 2% loss = 9,800
    result = check(EntryOrder("BTC/USDT", 100.0), acct)
    assert (result == Approved()) is ok
    if not ok:
        assert isinstance(result, Rejected) and "daily loss" in result.reason


# --- order rate ------------------------------------------------------------------------------


def test_order_rate_boundary() -> None:
    now = CLOSE + pd.Timedelta(seconds=10)
    five = tuple(now - pd.Timedelta(minutes=m) for m in range(5))
    six = (*five, now - pd.Timedelta(minutes=59))
    assert check(recent=five, now=now) == Approved()  # this would be the 6th in the hour
    result = check(recent=six, now=now)
    assert isinstance(result, Rejected) and "order rate" in result.reason
    expired = (*five, now - pd.Timedelta(hours=1))  # exactly an hour old: out of the window
    assert check(recent=expired, now=now) == Approved()


# --- data freshness --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "ok"),
    [
        (CLOSE, True),
        (CLOSE + pd.Timedelta(seconds=180), True),
        (CLOSE + pd.Timedelta(seconds=179), True),
        (CLOSE + pd.Timedelta(seconds=181), False),
    ],
)
def test_freshness_tolerance_boundary(now: pd.Timestamp, ok: bool) -> None:
    result = check(now=now)
    assert (result == Approved()) is ok
    if not ok:
        assert isinstance(result, Rejected) and "stale" in result.reason


def test_stale_data_previous_bar_rejected() -> None:
    # A new bar closed at CLOSE but we still only have the one before it.
    result = check(last_close=CLOSE - STEP, now=CLOSE + pd.Timedelta(seconds=5))
    assert isinstance(result, Rejected) and "stale" in result.reason


def test_bar_from_the_future_rejected() -> None:
    result = check(last_close=CLOSE + STEP, now=CLOSE + pd.Timedelta(seconds=5))
    assert isinstance(result, Rejected) and "stale" in result.reason


def test_naive_timestamps_rejected() -> None:
    with pytest.raises(ValueError):
        check(now=pd.Timestamp("2025-03-10 08:00:10"))
