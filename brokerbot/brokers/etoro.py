"""eToro adapter.

eToro opened public APIs and a Builders Portal in April 2026 - REST endpoints
plus a WebSocket feed, available to any verified account, in Sweden included.
That makes it the most *accessible* option here.

Two caveats that matter more than accessibility:

* **Cost.** eToro advertises zero commission, but the spread is the product,
  and non-USD deposits are converted at a markup. The ``etoro`` cost preset
  models a round trip at roughly 1.25%, against about 0.25% at Interactive
  Brokers. A strategy trading weekly needs to clear five times as much edge
  to break even. Run the backtest under both presets before choosing.
* **Verification.** API access requires a verified account, and the exact
  authentication scheme is set by eToro's portal rather than documented
  publicly, so the auth header here is configurable.

**Unverified from the build sandbox** - no credentials and no outbound access,
so this is written generically against an API-key REST shape. Check the
Builders Portal docs and adjust ``auth_header``/paths if they differ. The
``--check`` command reports exactly which call failed.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Order, OrderStatus, Side
from .base import AccountSummary, Broker, BrokerPosition, OrderResult

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.etoro.com"


class EtoroBroker(Broker):
    name = "etoro"
    supports_live = True

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE,
                 auth_header: str = "X-API-KEY", demo: bool = True,
                 paths: dict[str, str] | None = None) -> None:
        if not api_key:
            raise ValueError("eToro requires an API key from the Builders Portal")
        self.api_key = api_key
        self.base = base_url.rstrip("/")
        self.auth_header = auth_header
        self.demo = demo
        self.paths = {
            "account": "/api/v1/account/balance",
            "positions": "/api/v1/account/positions",
            "price": "/api/v1/market/quote",
            "order": "/api/v1/trading/order",
            **(paths or {}),
        }
        self._session: Any = None
        if not demo:
            log.warning("eToro REAL account mode: orders will use real money.")

    async def _sess(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={self.auth_header: self.api_key,
                         "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=25),
            )
        return self._session

    async def _req(self, method: str, path: str, **kwargs) -> Any:
        sess = await self._sess()
        try:
            async with sess.request(method, f"{self.base}{path}", **kwargs) as resp:
                if resp.status == 401:
                    raise RuntimeError(
                        "eToro returned 401 - check the API key and that the "
                        f"auth header name is right (currently {self.auth_header!r})"
                    )
                if resp.status >= 400:
                    log.error("eToro %s %s -> %s: %s", method, path, resp.status,
                              (await resp.text())[:200])
                    return None
                return await resp.json(content_type=None)
        except RuntimeError:
            raise
        except Exception as exc:
            log.error("eToro %s %s failed: %s", method, path, exc)
            return None

    async def connect(self) -> bool:
        data = await self._req("GET", self.paths["account"])
        if data is None:
            log.error(
                "could not reach eToro. Verify the base URL and paths against "
                "the Builders Portal docs at https://api-portal.etoro.com/"
            )
            return False
        log.info("connected to eToro (%s)", "demo" if self.demo else "REAL")
        return True

    async def account(self) -> AccountSummary:
        data = await self._req("GET", self.paths["account"]) or {}
        return AccountSummary(
            cash=float(data.get("available") or data.get("cash") or 0.0),
            equity=float(data.get("equity") or data.get("balance") or 0.0),
            currency=data.get("currency", "USD"),
            buying_power=float(data.get("available") or 0.0),
        )

    async def positions(self) -> list[BrokerPosition]:
        data = await self._req("GET", self.paths["positions"])
        items = data if isinstance(data, list) else (data or {}).get("positions", [])
        out: list[BrokerPosition] = []
        for item in items or []:
            out.append(BrokerPosition(
                symbol=str(item.get("instrument") or item.get("symbol", "")),
                quantity=float(item.get("units") or item.get("quantity") or 0.0),
                avg_price=float(item.get("openRate") or item.get("avgPrice") or 0.0),
                market_value=float(item.get("value") or 0.0),
                unrealised_pnl=float(item.get("profit") or 0.0),
                currency=item.get("currency", "USD"),
            ))
        return out

    async def last_price(self, symbol: str) -> float | None:
        data = await self._req("GET", self.paths["price"], params={"symbol": symbol})
        if not data:
            return None
        for key in ("last", "price", "mid", "ask"):
            if data.get(key):
                return float(data[key])
        return None

    async def place(self, order: Order) -> OrderResult:
        payload = {
            "symbol": order.symbol,
            "side": "BUY" if order.side is Side.BUY else "SELL",
            "units": abs(order.quantity),
            "orderType": order.order_type.value.upper(),
            "isDemo": self.demo,
        }
        if order.limit_price:
            payload["rate"] = order.limit_price
        data = await self._req("POST", self.paths["order"], json=payload)
        if not data:
            return OrderResult(False, OrderStatus.REJECTED, detail="order rejected or unreachable")
        return OrderResult(
            True, OrderStatus.PENDING,
            broker_order_id=str(data.get("orderId") or data.get("id") or ""),
            detail="order submitted",
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
