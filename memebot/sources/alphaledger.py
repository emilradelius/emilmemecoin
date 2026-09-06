"""AlphaLedger signals - majors, and market regime.

Two honest caveats up front:

1. **AlphaLedger.ai does not publish a documented public API** as far as I
   could verify. This adapter is therefore written as a *generic* poller: you
   point it at an endpoint and describe the response shape in ``config.yaml``,
   and it maps that into Signals. If AlphaLedger exposes an API to you (or
   adds one), filling in the mapping is a config change, not a code change.
   If it never does, the adapter stays dormant and the rest of the bot runs
   on two sources. See ``docs/ALPHALEDGER.md``.

2. **AlphaLedger covers majors, not meme coins.** A BTC or SOL position tells
   you nothing about whether a three-hour-old dog coin is going to work. So
   this source is deliberately *not* wired as a primary trigger. It does two
   narrower jobs, both of which are genuinely useful:

   * **Corroboration** on the rare occasions a token appears on both.
   * **Regime detection** - when the platform's top-ranked traders are net
     de-risking on majors, meme coin risk appetite is usually about to dry
     up. In that state the consensus engine raises its thresholds rather than
     going silent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import Config
from ..http import HttpClient
from ..models import Side, Signal, Source
from .base import SignalSource

log = logging.getLogger(__name__)

# Default field mapping, overridable in config under
# sources.alphaledger.field_map. Keys are our field names, values are the
# names (or dotted paths) to read from each item in the provider's response.
DEFAULT_FIELD_MAP = {
    "items_path": "data",
    "actor": "trader.username",
    "symbol": "symbol",
    "side": "side",
    "size_usd": "notional_usd",
    "price": "price",
    "timestamp": "executed_at",
    "url": "url",
}


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


class AlphaLedgerSource(SignalSource):
    name = "alphaledger"

    def __init__(
        self,
        cfg: Config,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> None:
        super().__init__()
        a = cfg.section("sources").get("alphaledger", {})
        self.enabled = a.get("enabled", False)
        self.poll_interval = a.get("poll_interval_seconds", 300)
        self.use_as_regime_filter = a.get("use_as_regime_filter", True)
        self.field_map = {**DEFAULT_FIELD_MAP, **(a.get("field_map") or {})}
        self.endpoint = a.get("endpoint", "/v1/trades/recent")

        self.api_key = api_key
        self.api_base = (api_base or "").rstrip("/")
        self.http = HttpClient(
            rate=1.0,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
        )

        self._seen: set[str] = set()
        # Rolling regime state, read by the consensus engine.
        self._regime_risk_off = False
        self._regime_updated_at = 0.0

    @property
    def risk_off(self) -> bool:
        """True when majors look risk-off. Stale state (>6h) decays to False
        so a dead feed cannot silently suppress the bot forever."""
        if time.time() - self._regime_updated_at > 6 * 3600:
            return False
        return self._regime_risk_off

    async def run(self, out: asyncio.Queue[Signal]) -> None:
        if not self.enabled:
            log.info("alphaledger source disabled in config")
            return
        if not self.api_key or not self.api_base:
            log.warning(
                "alphaledger enabled but ALPHALEDGER_API_KEY/ALPHALEDGER_API_BASE "
                "are not set - staying dormant. See docs/ALPHALEDGER.md."
            )
            return

        while True:
            try:
                items = await self._fetch()
                signals = [s for s in (self._parse(i) for i in items) if s]
                for sig in signals:
                    await self.emit(out, sig)
                self._update_regime(signals)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("alphaledger poll failed; continuing")
            await asyncio.sleep(self.poll_interval)

    async def _fetch(self) -> list[dict[str, Any]]:
        data = await self.http.get_json(f"{self.api_base}{self.endpoint}", use_cache=False)
        if not data:
            return []
        items = _dig(data, self.field_map["items_path"]) if isinstance(data, dict) else data
        return items if isinstance(items, list) else []

    def _parse(self, item: dict[str, Any]) -> Signal | None:
        fm = self.field_map
        actor = _dig(item, fm["actor"])
        symbol = _dig(item, fm["symbol"])
        side_raw = str(_dig(item, fm["side"]) or "").lower()
        if not actor or not symbol or side_raw not in {"buy", "sell", "long", "short"}:
            return None

        dedupe = f"{actor}:{symbol}:{_dig(item, fm['timestamp'])}"
        if dedupe in self._seen:
            return None
        self._seen.add(dedupe)
        if len(self._seen) > 20_000:
            self._seen = set(list(self._seen)[-10_000:])

        size = _dig(item, fm["size_usd"])
        price = _dig(item, fm["price"])

        return Signal(
            source=Source.ALPHALEDGER,
            actor_id=str(actor),
            # Majors have no Solana mint. The cex: namespace keeps them from
            # ever colliding with a meme coin mint in the consensus engine.
            token_mint=f"cex:{str(symbol).upper()}",
            token_symbol=str(symbol).upper(),
            side=Side.BUY if side_raw in {"buy", "long"} else Side.SELL,
            size_usd=float(size) if size is not None else None,
            price_usd=float(price) if price is not None else None,
            actor_score=0.6,
            confidence=0.9,
            url=_dig(item, fm["url"]),
            raw=item,
        )

    def _update_regime(self, signals: list[Signal]) -> None:
        """Net direction of top traders on majors over the last batch."""
        if not self.use_as_regime_filter or not signals:
            return
        majors = {"BTC", "ETH", "SOL"}
        relevant = [s for s in signals if (s.token_symbol or "") in majors]
        if len(relevant) < 3:
            return
        buys = sum(1 for s in relevant if s.side is Side.BUY)
        sells = len(relevant) - buys
        was = self._regime_risk_off
        # Two-thirds selling on majors is the risk-off trigger.
        self._regime_risk_off = sells > buys * 2
        self._regime_updated_at = time.time()
        if was != self._regime_risk_off:
            log.info(
                "market regime -> %s (%d buys / %d sells on majors)",
                "RISK-OFF" if self._regime_risk_off else "risk-on", buys, sells,
            )

    async def close(self) -> None:
        await self.http.close()
