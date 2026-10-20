"""Alerts and a dead-man's-switch ping. Paper only; no secrets in the repo.

Configuration comes only from environment variables:
- TRADER_ALERT_WEBHOOK: URL that receives a POST per alert. The JSON body carries the
  message as both "text" (Slack) and "content" (Discord); set TRADER_ALERT_FORMAT=ntfy to
  send a plain-text body with a Title header instead (ntfy.sh push notifications).
- TRADER_DEADMAN_URL: URL fetched on every wake (e.g. healthchecks.io); its service alerts
  you when the pings stop, which covers a dead machine or loop.

URLs are never logged. Alerting failures are logged and swallowed: they must never stop
the loop.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)

Sender = Callable[[urllib.request.Request], None]


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
        self.sent: list[tuple[str, str]] = []  # kept for status/tests; never includes URLs

    @classmethod
    def from_env(cls) -> Alerter:
        return cls(
            os.environ.get("TRADER_ALERT_WEBHOOK") or None,
            os.environ.get("TRADER_DEADMAN_URL") or None,
            os.environ.get("TRADER_ALERT_FORMAT", "json"),
        )

    def alert(self, title: str, message: str) -> None:
        self.sent.append((title, message))
        log.warning("ALERT %s: %s", title, message)
        if not self.webhook:
            return
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
