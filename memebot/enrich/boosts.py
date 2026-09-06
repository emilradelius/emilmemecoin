"""DexScreener Boosts: detecting tokens that are paying for attention.

A "boost" is a payment made to DexScreener to promote a token's listing. That
payment comes from whoever is behind the token, which makes it one of the few
directly observable signals of **marketing intent** in this market.

This is a *negative* signal, and that framing is the whole point. The naive
reading is that a boosted token is a trending token worth buying. The accurate
reading is that someone is spending money to put it in front of retail buyers,
which is what you would do if you needed exit liquidity. A genuinely organic
move does not need to buy visibility.

It is not disqualifying on its own - legitimate projects do sometimes boost -
so it applies a penalty scaled by how much was spent and how young the token
is. A brand-new token with a large boost is the combination that matters: it
means the promotion budget existed before the community did.

The endpoints are free and rate-limited to about 60 requests/minute, so a
single poll every couple of minutes covers everything at negligible cost.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..http import HttpClient

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
LATEST = f"{BASE}/token-boosts/latest/v1"
TOP = f"{BASE}/token-boosts/top/v1"


@dataclass(slots=True)
class BoostInfo:
    mint: str
    amount: float = 0.0
    """Most recent boost purchase."""

    total_amount: float = 0.0
    """Cumulative boosts bought for this token."""

    seen_at: float = 0.0

    @property
    def is_boosted(self) -> bool:
        return self.total_amount > 0


class BoostTracker:
    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        chain: str = "solana",
        heavy_boost_threshold: float = 500.0,
        max_penalty: float = 0.35,
        refresh_seconds: float = 120.0,
    ) -> None:
        self.http = client or HttpClient(rate=1.0, cache_ttl=60.0)
        self.chain = chain
        self.heavy_boost_threshold = heavy_boost_threshold
        self.max_penalty = max_penalty
        self.refresh_seconds = refresh_seconds

        self._boosts: dict[str, BoostInfo] = {}
        self._last_refresh = 0.0

    async def close(self) -> None:
        await self.http.close()

    async def refresh(self, *, force: bool = False) -> int:
        if not force and time.time() - self._last_refresh < self.refresh_seconds:
            return len(self._boosts)

        found = 0
        for url in (LATEST, TOP):
            data = await self.http.get_json(url, use_cache=False)
            if not isinstance(data, list):
                # Some deployments wrap the array; accept either shape.
                data = (data or {}).get("data") if isinstance(data, dict) else None
                if not isinstance(data, list):
                    continue
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if entry.get("chainId") != self.chain:
                    continue
                mint = entry.get("tokenAddress")
                if not mint:
                    continue
                try:
                    amount = float(entry.get("amount") or 0)
                    total = float(entry.get("totalAmount") or amount)
                except (TypeError, ValueError):
                    continue
                existing = self._boosts.get(mint)
                # Keep the largest cumulative figure seen across endpoints.
                if existing is None or total > existing.total_amount:
                    self._boosts[mint] = BoostInfo(
                        mint=mint, amount=amount, total_amount=total,
                        seen_at=time.time(),
                    )
                found += 1

        self._last_refresh = time.time()
        # Bound memory; boosts age out of relevance quickly.
        if len(self._boosts) > 5000:
            cutoff = time.time() - 86400
            self._boosts = {
                k: v for k, v in self._boosts.items() if v.seen_at >= cutoff
            }
        log.debug("boost tracker refreshed: %d entries", len(self._boosts))
        return found

    def get(self, mint: str) -> BoostInfo | None:
        return self._boosts.get(mint)

    def penalty(self, mint: str, *, age_minutes: float | None = None) -> tuple[float, str | None]:
        """Return a conviction multiplier in (0, 1] and a human-readable note.

        Scales with spend, and is amplified for very young tokens - a big
        promotion budget on a token that is an hour old means the marketing
        was arranged before there was anything to market.
        """
        info = self._boosts.get(mint)
        if info is None or not info.is_boosted:
            return 1.0, None

        severity = min(1.0, info.total_amount / self.heavy_boost_threshold)
        if age_minutes is not None and age_minutes < 360:
            severity = min(1.0, severity * 1.5)

        multiplier = round(1.0 - self.max_penalty * severity, 3)
        note = (
            f"paid DexScreener boost ({info.total_amount:.0f} total) - "
            f"someone is buying visibility for this token"
        )
        return multiplier, note
