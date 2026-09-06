"""Cached SOL/USD price.

Needed to convert on-chain SOL amounts into the USD position sizes the
consensus engine's size factor expects. Cached for a minute; SOL does not
move enough in sixty seconds to change a position-size bucket.
"""

from __future__ import annotations

import logging
import time

from .dexscreener import DexScreener

log = logging.getLogger(__name__)

WSOL = "So11111111111111111111111111111111111111112"


class SolPrice:
    def __init__(self, dex: DexScreener, ttl: float = 60.0) -> None:
        self.dex = dex
        self.ttl = ttl
        self._price: float | None = None
        self._at = 0.0

    async def get(self) -> float | None:
        if self._price is not None and time.time() - self._at < self.ttl:
            return self._price
        market = await self.dex.get(WSOL)
        if market and market.price_usd:
            self._price = market.price_usd
            self._at = time.time()
        elif self._price is None:
            log.warning("could not fetch SOL price; USD sizing unavailable")
        return self._price
