"""Kill switch (SPEC section 6.2-6.3): persisted, sticky, fails closed.

State transitions are pure functions on an immutable `State`; `load`/`save` persist it as
JSON with atomic writes. A missing, unreadable or invalid state file loads as TRIPPED, so a
fresh install starts tripped and must be armed explicitly. Once tripped, nothing but an
explicit `rearm` (CLI, with a reason, written to the audit log) clears it.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


@dataclass(frozen=True)
class KillSwitchLimits:
    max_drawdown_pct: float
    max_consecutive_api_errors: int

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> KillSwitchLimits:
        r = cfg["risk"]
        return cls(r["max_drawdown_pct"], r["max_consecutive_api_errors"])


@dataclass(frozen=True)
class State:
    tripped: bool
    reason: str
    peak_equity: float
    consecutive_api_errors: int
    updated_at: str  # ISO UTC


_FIELDS: dict[str, type | tuple[type, ...]] = {
    "tripped": bool,
    "reason": str,
    "peak_equity": (int, float),
    "consecutive_api_errors": int,
    "updated_at": str,
}


def _tripped(reason: str) -> State:
    return State(True, reason, 0.0, 0, "")


# --- transitions -----------------------------------------------------------------------------


def trip(state: State, reason: str, now: pd.Timestamp) -> State:
    if state.tripped:
        return state  # keep the first reason
    return replace(state, tripped=True, reason=reason, updated_at=now.isoformat())


def on_equity(state: State, equity: float, now: pd.Timestamp, limits: KillSwitchLimits) -> State:
    """Track peak equity; trip when the drawdown from peak reaches the limit."""
    if state.tripped:
        return state
    peak = max(state.peak_equity, equity)
    state = replace(state, peak_equity=peak, updated_at=now.isoformat())
    if peak - equity >= peak * limits.max_drawdown_pct / 100:
        return trip(
            state, f"drawdown {100 * (peak - equity) / peak:.2f}% from peak {peak:.2f}", now
        )
    return state


def on_api_error(state: State, now: pd.Timestamp, limits: KillSwitchLimits) -> State:
    if state.tripped:
        return state
    errors = state.consecutive_api_errors + 1
    state = replace(state, consecutive_api_errors=errors, updated_at=now.isoformat())
    if errors >= limits.max_consecutive_api_errors:
        return trip(state, f"{errors} consecutive API errors", now)
    return state


def on_api_success(state: State) -> State:
    return state if state.tripped else replace(state, consecutive_api_errors=0)


def rearm(state: State, reason: str, now: pd.Timestamp, equity: float) -> State:
    """Explicit operator action. The peak restarts at current equity so an old drawdown
    does not re-trip immediately."""
    if not reason.strip():
        raise ValueError("re-arming requires a reason")
    if equity <= 0:
        raise ValueError("re-arming requires positive current equity")
    return State(False, f"re-armed: {reason.strip()}", equity, 0, now.isoformat())


# --- persistence -----------------------------------------------------------------------------


def load(path: Path) -> State:
    """Read state; anything other than a valid file yields a TRIPPED state."""
    if not path.exists():
        return _tripped(f"state file missing: {path}")
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError) as e:
        return _tripped(f"state file unreadable: {e}")
    except json.JSONDecodeError as e:
        return _tripped(f"state file corrupt: {e}")
    if not isinstance(raw, dict) or set(raw) != set(_FIELDS):
        return _tripped("state file corrupt: unexpected fields")
    for key, typ in _FIELDS.items():
        # bool is a subclass of int; reject it where a number is expected
        if not isinstance(raw[key], typ) or (typ is not bool and isinstance(raw[key], bool)):
            return _tripped(f"state file corrupt: bad type for {key}")
    return State(
        tripped=raw["tripped"],
        reason=raw["reason"],
        peak_equity=float(raw["peak_equity"]),
        consecutive_api_errors=raw["consecutive_api_errors"],
        updated_at=raw["updated_at"],
    )


def save(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(asdict(state), f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def audit_path(path: Path) -> Path:
    return path.with_suffix(".audit.jsonl")


def record(path: Path, event: str, reason: str, now: pd.Timestamp) -> None:
    """Append an operator or automatic event to the audit log next to the state file."""
    line = json.dumps({"time": now.isoformat(), "event": event, "reason": reason})
    with audit_path(path).open("a") as f:
        f.write(line + "\n")


# --- CLI -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kill switch status, manual trip and re-arm.")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--state", type=Path, help="state file (default: from config)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p_trip = sub.add_parser("trip")
    p_trip.add_argument("--reason", required=True)
    p_rearm = sub.add_parser("rearm")
    p_rearm.add_argument("--reason", default="")
    p_rearm.add_argument("--equity", type=float, required=True, help="current equity")
    args = parser.parse_args(argv)

    path = args.state or Path(yaml.safe_load(args.config.read_text())["risk"]["killswitch_state"])
    now = pd.Timestamp.now(tz="UTC")
    state = load(path)
    if args.command == "trip":
        state = trip(state, f"manual: {args.reason}", now)
        save(path, state)
        record(path, "trip", args.reason, now)
    elif args.command == "rearm":
        if not args.reason.strip():
            print("refusing to re-arm without --reason")
            return 2
        state = rearm(state, args.reason, now, args.equity)
        save(path, state)
        record(path, "rearm", args.reason, now)
    print(f"kill switch {'tripped' if state.tripped else 'armed'}: {state.reason} [{path}]")
    return 1 if args.command == "status" and state.tripped else 0


if __name__ == "__main__":
    raise SystemExit(main())
