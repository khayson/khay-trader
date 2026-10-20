"""Live broker: deliberately not implemented. Live trading needs the SPEC section 9 gate,
a private repo and exchange credentials, none of which exist yet."""

from __future__ import annotations

from typing import Any


class LiveBroker:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("live trading is not implemented (SPEC section 9 gate)")
