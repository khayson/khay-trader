"""SQLite journal for the paper loop: the single source of truth for its state.

Each loop wake writes inside one transaction, so a crash mid-wake leaves the previous
consistent state and the restart redoes that wake. Timestamps are stored as ISO UTC text.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from trader.exec.base import Fill

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY, qty REAL NOT NULL, entry_price REAL NOT NULL,
    entry_time TEXT NOT NULL, entry_cost REAL NOT NULL, stop_price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, wake_time TEXT, bar_close TEXT, symbol TEXT, features TEXT,
    action TEXT, size REAL, stop_price REAL, risk TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, time TEXT, signal_time TEXT, symbol TEXT, side TEXT, qty REAL,
    price REAL, modeled_price REAL, raw_price REAL, fee REAL, reason TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, symbol TEXT, entry_time TEXT, exit_time TEXT, qty REAL,
    entry_price REAL, exit_price REAL, entry_cost REAL, pnl REAL, exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    time TEXT PRIMARY KEY, equity REAL, cash REAL, exposure REAL
);
CREATE TABLE IF NOT EXISTS parity (
    id INTEGER PRIMARY KEY, checked_at TEXT, symbol TEXT, bar_open TEXT, field TEXT,
    ours REAL, native REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, time TEXT, kind TEXT, message TEXT
);
"""


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    qty: float
    entry_price: float
    entry_time: pd.Timestamp
    entry_cost: float
    stop_price: float


def _iso(ts: pd.Timestamp | None) -> str | None:
    return None if ts is None else pd.Timestamp(ts).isoformat()


class Journal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --- key/value state ---------------------------------------------------------------

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set(self, key: str, value: Any) -> None:
        text = value.isoformat() if isinstance(value, pd.Timestamp) else str(value)
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, text),
        )

    def get_time(self, key: str) -> pd.Timestamp | None:
        value = self.get(key)
        return None if value is None else pd.Timestamp(value)

    def get_float(self, key: str) -> float | None:
        value = self.get(key)
        return None if value is None else float(value)

    # --- positions ---------------------------------------------------------------------

    def positions(self) -> dict[str, PositionRow]:
        rows = self.conn.execute(
            "SELECT symbol, qty, entry_price, entry_time, entry_cost, stop_price FROM positions"
        ).fetchall()
        return {r[0]: PositionRow(r[0], r[1], r[2], pd.Timestamp(r[3]), r[4], r[5]) for r in rows}

    def upsert_position(self, p: PositionRow) -> None:
        self.conn.execute(
            "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET "
            "qty = excluded.qty, entry_price = excluded.entry_price, "
            "entry_time = excluded.entry_time, entry_cost = excluded.entry_cost, "
            "stop_price = excluded.stop_price",
            (p.symbol, p.qty, p.entry_price, _iso(p.entry_time), p.entry_cost, p.stop_price),
        )

    def delete_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))

    # --- append-only records -----------------------------------------------------------

    def add_fill(self, f: Fill, signal_time: pd.Timestamp | None) -> None:
        self.conn.execute(
            "INSERT INTO fills (time, signal_time, symbol, side, qty, price, modeled_price, "
            "raw_price, fee, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(f.time),
                _iso(signal_time),
                f.symbol,
                f.side,
                f.qty,
                f.price,
                f.modeled_price,
                f.raw_price,
                f.fee,
                f.reason,
            ),
        )

    def add_trade(self, p: PositionRow, exit_fill: Fill, pnl: float) -> None:
        self.conn.execute(
            "INSERT INTO trades (symbol, entry_time, exit_time, qty, entry_price, exit_price, "
            "entry_cost, pnl, exit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p.symbol,
                _iso(p.entry_time),
                _iso(exit_fill.time),
                p.qty,
                p.entry_price,
                exit_fill.price,
                p.entry_cost,
                pnl,
                exit_fill.reason,
            ),
        )

    def add_decision(
        self,
        wake: pd.Timestamp,
        bar_close: pd.Timestamp,
        symbol: str,
        features: dict[str, float] | None,
        action: str,
        size: float,
        stop_price: float | None,
        risk: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO decisions (wake_time, bar_close, symbol, features, action, size, "
            "stop_price, risk) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(wake),
                _iso(bar_close),
                symbol,
                json.dumps(features),
                action,
                size,
                stop_price,
                risk,
            ),
        )

    def set_risk(self, decision_id: int, risk: str) -> None:
        self.conn.execute("UPDATE decisions SET risk = ? WHERE id = ?", (risk, decision_id))

    def last_id(self) -> int:
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def add_equity(self, time: pd.Timestamp, equity: float, cash: float, exposure: float) -> None:
        self.conn.execute(
            "INSERT INTO equity VALUES (?, ?, ?, ?) ON CONFLICT(time) DO UPDATE SET "
            "equity = excluded.equity, cash = excluded.cash, exposure = excluded.exposure",
            (_iso(time), equity, cash, exposure),
        )

    def add_parity(
        self,
        checked_at: pd.Timestamp,
        symbol: str,
        bar_open: pd.Timestamp,
        field: str,
        ours: float | None,
        native: float | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO parity (checked_at, symbol, bar_open, field, ours, native) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_iso(checked_at), symbol, _iso(bar_open), field, ours, native),
        )

    def add_event(self, time: pd.Timestamp, kind: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO events (time, kind, message) VALUES (?, ?, ?)", (_iso(time), kind, message)
        )

    # --- reads -------------------------------------------------------------------------

    def frame(self, table: str, order: str = "id") -> pd.DataFrame:
        if table not in {"decisions", "fills", "trades", "equity", "parity", "events"}:
            raise ValueError(table)
        df = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY {order}", self.conn)
        for col in (
            "time",
            "signal_time",
            "entry_time",
            "exit_time",
            "wake_time",
            "bar_close",
            "bar_open",
            "checked_at",
        ):
            if col in df:
                df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
        return df
