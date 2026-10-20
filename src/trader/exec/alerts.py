"""Alerts and a dead-man's-switch ping. Paper only; no secrets in the repo.

Configuration comes only from environment variables:
- TRADER_ALERT_WEBHOOK: URL that receives a POST per alert. The JSON body carries the
  message as both "text" (Slack) and "content" (Discord); set TRADER_ALERT_FORMAT=ntfy to
  send a plain-text body with a Title header instead (ntfy.sh push notifications).
- TRADER_DEADMAN_URL: URL fetched every minute (e.g. healthchecks.io); its service alerts
  you when the pings stop, which covers a dead machine or loop.

`alert()` only queues; `flush()` delivers, and the loop calls it after each wake or tick,
so a slow webhook never holds a journal transaction open. URLs are never logged.
Delivery failures are logged and swallowed: alerting must never stop the loop.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)

Sender = Callable[[urllib.request.Request], None]
SENT_HISTORY = 200


def _send(request: urllib.request.Request) -> None:
    with urllib.request.urlopen(request, timeout=10):  # noqa: S310 (URLs come from env)
        pass


class Alerter:
    def __init__(
        self,
        webhook: str | None = None,
        deadman: str | None = None,
        fmt: str = "json",
        sender: Sender = _send,
    ) -> None:
        self.webhook, self.deadman, self.fmt, self.sender = webhook, deadman, fmt, sender
        self.pending: list[tuple[str, str]] = []
        # Recently delivered (title, message) pairs, for status and tests; never URLs.
        self.sent: deque[tuple[str, str]] = deque(maxlen=SENT_HISTORY)

    @classmethod
    def from_env(cls) -> Alerter:
        return cls(
            os.environ.get("TRADER_ALERT_WEBHOOK") or None,
            os.environ.get("TRADER_DEADMAN_URL") or None,
            os.environ.get("TRADER_ALERT_FORMAT", "json"),
        )

    def alert(self, title: str, message: str) -> None:
        """Queue an alert; it is logged now and delivered on the next flush()."""
        log.warning("ALERT %s: %s", title, message)
        self.pending.append((title, message))

    def flush(self) -> None:
        pending, self.pending = self.pending, []
        for title, message in pending:
            self.sent.append((title, message))
            if self.webhook:
                self._deliver(title, message)

    def _deliver(self, title: str, message: str) -> None:
        assert self.webhook is not None
        if self.fmt == "ntfy":
            request = urllib.request.Request(
                self.webhook, data=message.encode(), headers={"Title": f"khay-trader: {title}"}
            )
        else:
            text = f"khay-trader: {title}: {message}"
            request = urllib.request.Request(
                self.webhook,
                data=json.dumps({"text": text, "content": text}).encode(),
                headers={"Content-Type": "application/json"},
            )
        try:
            self.sender(request)
        except Exception as e:  # never let alerting break trading
            log.error("alert delivery failed: %s", type(e).__name__)

    def ping(self) -> None:
        if not self.deadman:
            return
        try:
            self.sender(urllib.request.Request(self.deadman))
        except Exception as e:
            log.error("dead-man ping failed: %s", type(e).__name__)
