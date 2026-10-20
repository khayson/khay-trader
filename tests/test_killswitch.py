import json
from pathlib import Path

import pandas as pd
import pytest

from trader.risk import killswitch as ks
from trader.risk.killswitch import KillSwitchLimits, State

NOW = pd.Timestamp("2025-03-10 08:00", tz="UTC")
LIM = KillSwitchLimits(max_drawdown_pct=15, max_consecutive_api_errors=5)


def armed(peak: float = 10_000.0) -> State:
    return ks.rearm(ks.load(Path("does-not-exist.json")), "test setup", NOW, equity=peak)


# --- fail closed -----------------------------------------------------------------------------


def test_missing_state_fails_closed(tmp_path: Path) -> None:
    state = ks.load(tmp_path / "killswitch.json")
    assert state.tripped and "missing" in state.reason


def test_corrupt_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "killswitch.json"
    path.write_text("{not json")
    assert ks.load(path).tripped
    path.write_text(json.dumps({"tripped": False}))  # parses, but fields missing
    assert ks.load(path).tripped
    path.write_text(
        json.dumps(
            {
                "tripped": "no",
                "reason": "",
                "peak_equity": 1.0,
                "consecutive_api_errors": 0,
                "updated_at": NOW.isoformat(),
            }
        )
    )
    assert ks.load(path).tripped  # wrong type


def test_unreadable_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "killswitch.json"
    path.mkdir()  # a directory cannot be read as a file
    state = ks.load(path)
    assert state.tripped and "unreadable" in state.reason


# --- persistence -----------------------------------------------------------------------------


def test_restart_persistence(tmp_path: Path) -> None:
    path = tmp_path / "state" / "killswitch.json"
    ks.save(path, armed())
    assert not ks.load(path).tripped
    tripped = ks.trip(ks.load(path), "manual: operator", NOW)
    ks.save(path, tripped)
    reloaded = ks.load(path)  # a restart
    assert reloaded == tripped
    assert reloaded.tripped and reloaded.reason == "manual: operator"
    assert not list(path.parent.glob("*.tmp"))  # atomic write leaves no temp file


# --- drawdown --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("equity", "trips"), [(8_500.01, False), (8_500.0, True), (8_499.99, True)]
)
def test_drawdown_boundary(equity: float, trips: bool) -> None:
    state = ks.on_equity(armed(10_000.0), equity, NOW, LIM)
    assert state.tripped is trips
    if trips:
        assert "drawdown" in state.reason


def test_peak_ratchets_up_only() -> None:
    state = ks.on_equity(armed(10_000.0), 12_000.0, NOW, LIM)
    assert state.peak_equity == 12_000.0
    state = ks.on_equity(state, 11_000.0, NOW, LIM)
    assert state.peak_equity == 12_000.0 and not state.tripped
    assert ks.on_equity(state, 10_200.0, NOW, LIM).tripped  # 15% below the new peak


def test_drawdown_breach_after_new_peak_trips() -> None:
    state = armed(10_000.0)
    for equity in [10_500.0, 11_000.0, 9_900.0, 9_350.0]:
        state = ks.on_equity(state, equity, NOW, LIM)
    assert state.tripped  # 9,350 is exactly 15% below 11,000


# --- API errors ------------------------------------------------------------------------------


def test_api_error_burst_trips_at_limit() -> None:
    state = armed()
    for _ in range(4):
        state = ks.on_api_error(state, NOW, LIM)
    assert not state.tripped and state.consecutive_api_errors == 4
    state = ks.on_api_error(state, NOW, LIM)
    assert state.tripped and "API errors" in state.reason


def test_api_success_resets_the_count() -> None:
    state = armed()
    for _ in range(4):
        state = ks.on_api_error(state, NOW, LIM)
    state = ks.on_api_success(state)
    for _ in range(4):
        state = ks.on_api_error(state, NOW, LIM)
    assert not state.tripped


# --- tripped stays tripped until re-armed ----------------------------------------------------


def test_trip_is_sticky() -> None:
    state = ks.trip(armed(), "manual", NOW)
    state = ks.on_equity(state, 50_000.0, NOW, LIM)
    state = ks.on_api_success(state)
    assert state.tripped and state.reason == "manual"


def test_rearm_requires_reason_and_resets_peak() -> None:
    state = ks.on_equity(armed(10_000.0), 8_000.0, NOW, LIM)
    with pytest.raises(ValueError):
        ks.rearm(state, "  ", NOW, equity=8_000.0)
    rearmed = ks.rearm(state, "reviewed drawdown", NOW, equity=8_000.0)
    assert not rearmed.tripped and rearmed.peak_equity == 8_000.0
    assert rearmed.consecutive_api_errors == 0


# --- CLI and audit log -----------------------------------------------------------------------


def test_cli_rearm_trip_and_audit_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "killswitch.json"
    assert ks.main(["--state", str(path), "status"]) == 1  # missing -> tripped
    assert ks.main(["--state", str(path), "rearm", "--equity", "10000"]) == 2  # no reason
    assert (
        ks.main(["--state", str(path), "rearm", "--reason", "go paper", "--equity", "10000"]) == 0
    )
    assert not ks.load(path).tripped
    assert ks.main(["--state", str(path), "trip", "--reason", "drill"]) == 0
    assert ks.load(path).reason == "manual: drill"
    events = [json.loads(line) for line in ks.audit_path(path).read_text().splitlines()]
    assert [e["event"] for e in events] == ["rearm", "trip"]
    assert events[0]["reason"] == "go paper"
    assert "tripped" in capsys.readouterr().out
