"""DexScreener market data.

Free, no API key, and the most reliable source of the numbers the safety gate
actually cares about: pool liquidity, volume, price, and pair age. Documented
rate limits are generous (~300 req/min on the token endpoints), and the client
caches aggressively because the same mint gets looked at by the consensus
engine, the safety gate and the exit monitor within seconds of each other.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ..http import HttpClient

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"


@dataclass(slots=True)
class MarketData:
    mint: str
    symbol: str | None = None
    name: str | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    fdv_usd: float | None = None
    volume_24h_usd: float | None = None
    volume_6h_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_5m_usd: float | None = None
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    buys_6h: int = 0
    sells_6h: int = 0
    buys_24h: int = 0
    sells_24h: int = 0
    price_change_5m: float | None = None
    price_change_1h: float | None = None
    price_change_6h: float | None = None
    price_change_24h: float | None = None
    pair_created_at: float | None = None   # epoch seconds
    pair_address: str | None = None
    dex_id: str | None = None
    url: str | None = None

    @property
    def age_minutes(self) -> float | None:
        if self.pair_created_at is None:
            return None
        return max(0.0, (time.time() - self.pair_created_at) / 60.0)

    @property
    def volume_to_liquidity(self) -> float | None:
        if not self.liquidity_usd or not self.volume_24h_usd:
            return None
        return self.volume_24h_usd / self.liquidity_usd


def _f(d: dict[str, Any] | None, *path: str) -> float | None:
    node: Any = d
    for p in path:
        if not isinstance(node, dict) or p not in node:
            return None
        node = node[p]
    try:
        return float(node)
    except (TypeError, ValueError):
        return None


class DexScreener:
    def __init__(self, client: HttpClient | None = None) -> None:
        # 20s cache: fast enough for exit monitoring, slow enough to keep us
        # far under the rate limit when many components ask about one token.
        self.http = client or HttpClient(rate=4.0, cache_ttl=20.0)

    async def close(self) -> None:
        await self.http.close()

    @staticmethod
    def _best_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Pick the pair that actually matters: deepest liquidity.

        A token often has several pools (pump.fun bonding curve, Raydium,
        Meteora). Quoting the shallow one would make the safety gate reject
        good tokens and, worse, make price tracking wrong.
        """
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"] or pairs
        if not sol_pairs:
            return None
        return max(sol_pairs, key=lambda p: _f(p, "liquidity", "usd") or 0.0)

    @staticmethod
    def _txns(pair: dict[str, Any], window: str) -> tuple[int, int]:
        bucket = (pair.get("txns") or {}).get(window) or {}
        try:
            return int(bucket.get("buys", 0) or 0), int(bucket.get("sells", 0) or 0)
        except (TypeError, ValueError):
            return 0, 0

    def _parse(self, mint: str, pair: dict[str, Any]) -> MarketData:
        created_ms = pair.get("pairCreatedAt")
        b5, s5 = self._txns(pair, "m5")
        b1, s1 = self._txns(pair, "h1")
        b6, s6 = self._txns(pair, "h6")
        b24, s24 = self._txns(pair, "h24")
        return MarketData(
            mint=mint,
            symbol=(pair.get("baseToken") or {}).get("symbol"),
            name=(pair.get("baseToken") or {}).get("name"),
            price_usd=_f(pair, "priceUsd"),
            liquidity_usd=_f(pair, "liquidity", "usd"),
            fdv_usd=_f(pair, "fdv"),
            volume_24h_usd=_f(pair, "volume", "h24"),
            volume_6h_usd=_f(pair, "volume", "h6"),
            volume_1h_usd=_f(pair, "volume", "h1"),
            volume_5m_usd=_f(pair, "volume", "m5"),
            buys_5m=b5, sells_5m=s5,
            buys_1h=b1, sells_1h=s1,
            buys_6h=b6, sells_6h=s6,
            buys_24h=b24, sells_24h=s24,
            price_change_5m=_f(pair, "priceChange", "m5"),
            price_change_1h=_f(pair, "priceChange", "h1"),
            price_change_6h=_f(pair, "priceChange", "h6"),
            price_change_24h=_f(pair, "priceChange", "h24"),
            pair_created_at=(created_ms / 1000.0) if created_ms else None,
            pair_address=pair.get("pairAddress"),
            dex_id=pair.get("dexId"),
            url=pair.get("url"),
        )

    async def get(self, mint: str) -> MarketData | None:
        data = await self.http.get_json(f"{BASE}/latest/dex/tokens/{mint}")
        if not data:
            return None
        pairs = data.get("pairs") or []
        pair = self._best_pair(pairs)
        if not pair:
            return None
        return self._parse(mint, pair)

    async def get_many(self, mints: list[str]) -> dict[str, MarketData]:
        """Batch lookup. DexScreener accepts up to 30 comma-joined addresses."""
        out: dict[str, MarketData] = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i : i + 30]
            data = await self.http.get_json(
                f"{BASE}/latest/dex/tokens/{','.join(chunk)}"
            )
            if not data:
                continue
            by_mint: dict[str, list[dict[str, Any]]] = {}
            for pair in data.get("pairs") or []:
                addr = (pair.get("baseToken") or {}).get("address")
                if addr:
                    by_mint.setdefault(addr, []).append(pair)
            for mint, pairs in by_mint.items():
                best = self._best_pair(pairs)
                if best:
                    out[mint] = self._parse(mint, best)
        return out

    async def search(self, query: str) -> list[MarketData]:
        """Search by ticker or name. Used by the ticker resolver.

        Returns candidates sorted by liquidity, deepest first.
        """
        data = await self.http.get_json(f"{BASE}/latest/dex/search", {"q": query})
        if not data:
            return []
        results: list[MarketData] = []
        for pair in data.get("pairs") or []:
            if pair.get("chainId") != "solana":
                continue
            addr = (pair.get("baseToken") or {}).get("address")
            if not addr:
                continue
            results.append(self._parse(addr, pair))
        results.sort(key=lambda m: m.liquidity_usd or 0.0, reverse=True)
        return results
