"""Shared fixtures for the paper-loop tests."""

from __future__ import annotations

import pytest
from test_paper_loop import FIRST_WAKE, LAST_WAKE, World, make_world


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The candle fetcher's retry backoff sleeps for real; tests do not wait."""
    monkeypatch.setattr("trader.data.fetch.time.sleep", lambda _: None)


@pytest.fixture(scope="session")
def reference(tmp_path_factory: pytest.TempPathFactory) -> World:
    """An uninterrupted paper run over the synthetic data, shared by the paper tests."""
    w = make_world(tmp_path_factory.mktemp("reference"))
    w.run(FIRST_WAKE, LAST_WAKE)
    return w
