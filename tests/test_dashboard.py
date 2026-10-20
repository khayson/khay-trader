"""The read-only dashboard (tools/dashboard): renders the journal, cannot write to it,
and serves only on 127.0.0.1."""

from __future__ import annotations

import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools" / "dashboard"))
import dashboard  # noqa: E402

from trader.exec.base import Fill  # noqa: E402
from trader.exec.journal import Journal, PositionRow  # noqa: E402
from trader.risk import killswitch as ks  # noqa: E402

T0 = pd.Timestamp("2026-11-01", tz="UTC")
STEP = pd.Timedelta(hours=4)
NOW = T0 + 10 * STEP + pd.Timedelta(minutes=3)


@pytest.fixture
def journal_files(tmp_path: Path) -> tuple[Path, Path]:
    db, ks_path = tmp_path / "paper.db", tmp_path / "ks.json"
    j = Journal(db)
    with j.transaction():
        j.set("initial_equity", 500.0)
        j.set("cash", 450.0)
        j.set("paper_start", T0)
        j.set("heartbeat", NOW - pd.Timedelta(minutes=1))
        for k in range(10):
            j.add_equity(T0 + k * STEP, 500.0 + 3 * k, 450.0, 0.1)
            j.record_wake(T0 + k * STEP, "missed" if k == 4 else "processed", T0 + k * STEP)
        j.upsert_position(PositionRow("BTC/USDT", 0.0005, 100_000.0, T0, 50.05, 97_000.0))
        j.add_decision(
            NOW,
            T0 + 9 * STEP,
            "BTC/USDT",
            {"close": 101_000.0, "ema_fast": 100_500.0, "ema_slow": 99_000.0, "atr": 900.0},
            "hold",
            0.0,
            97_500.0,
            "",
        )
        j.add_decision(
            NOW,
            T0 + 9 * STEP,
            "ETH/USDT",
            None,
            "enter",
            0.01,
            3_000.0,
            "rejected: exchange minimum: notional 4.00 below 5.00",
        )
        j.add_fill(
            Fill(
                T0,
                "BTC/USDT",
                "buy",
                0.0005,
                100_060.0,
                100_050.0,
                100_000.0,
                0.05,
                "entry",
                100_020.0,
                4.0,
            ),
            signal_time=T0,
        )
        j.add_event(NOW, "outage", "<script>alert('x')</script> BTC/USDT no candles")
        j.add_event(NOW, "summary", "equity 527.00, positions: BTC/USDT")
    j.close()
    ks.save(ks_path, ks.rearm(ks.load(ks_path), "test", T0, 500.0))
    return db, ks_path


def test_renders_the_journal(journal_files: tuple[Path, Path]) -> None:
    db, ks_path = journal_files
    page = dashboard.render(dashboard.load(db, ks_path, 0.0005, NOW))
    assert "<title>Paper Trader Status</title>" in page
    assert "527.00" in page and "+5.40%" in page  # latest equity vs pinned 500
    assert "armed" in page and "BTC/USDT" in page and "97,000.00" in page
    assert "1 total, 1 in 7 days" in page  # one missed wake
    assert "rejected: exchange minimum" in page
    assert "Impact vs book mid" in page and "0.04%" in page  # 100,060 vs mid 100,020
    assert "equity 527.00, positions: BTC/USDT" in page  # daily summary
    assert "id='equity'" in page and "Table view" in page


def test_journal_text_is_escaped(journal_files: tuple[Path, Path]) -> None:
    db, ks_path = journal_files
    page = dashboard.render(dashboard.load(db, ks_path, 0.0005, NOW))
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page


def test_tripped_and_stale_states_are_flagged(journal_files: tuple[Path, Path]) -> None:
    db, ks_path = journal_files
    ks.save(ks_path, ks.trip(ks.load(ks_path), "manual: drill", NOW))
    page = dashboard.render(dashboard.load(db, ks_path, 0.0005, NOW + pd.Timedelta(hours=1)))
    assert "TRIPPED" in page and "manual: drill" in page
    assert "&#9888;" in page  # an icon with the label, not colour alone


def test_journal_is_opened_read_only(journal_files: tuple[Path, Path]) -> None:
    conn = dashboard.open_readonly(journal_files[0])
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM positions")
    conn.close()
    with pytest.raises(FileNotFoundError):
        dashboard.open_readonly(journal_files[0].parent / "missing.db")


def test_serves_get_only_on_localhost(journal_files: tuple[Path, Path]) -> None:
    db, ks_path = journal_files
    server = dashboard.make_server(
        lambda: dashboard.render(dashboard.load(db, ks_path, 0.0005, NOW)), port=0
    )
    host, port = server.server_address[:2]
    assert host == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
            assert response.status == 200 and b"Paper Trader Status" in response.read()
        for request in (
            urllib.request.Request(f"http://127.0.0.1:{port}/", data=b"x"),
            urllib.request.Request(f"http://127.0.0.1:{port}/admin"),
        ):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(request)
    finally:
        server.shutdown()
        server.server_close()


def test_older_journal_without_newer_tables_still_renders(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO kv VALUES ('cash', '100');"
    )
    conn.close()
    page = dashboard.render(dashboard.load(db, tmp_path / "ks.json", 0.0005, NOW))
    assert "No equity snapshots yet." in page and "TRIPPED" in page  # no state file
