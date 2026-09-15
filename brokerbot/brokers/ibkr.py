"""Interactive Brokers Client Portal Web API adapter.

The most capable option, and the cheapest to trade through - but the least
convenient to automate, for one specific reason worth knowing before you
choose it: **the API runs through a gateway on your own machine that must be
authenticated by a human in a browser, and the session expires daily.**

There is no headless credential flow for retail accounts. A bot on a VPS
will stop trading roughly every 24 hours until someone re-authenticates. That
is IBKR's deliberate design, not an oversight, and it makes unattended
operation genuinely awkward. :meth:`connect` checks the session explicitly so
you find out immediately rather than through silently missed trades.

Setup: download the Client Portal Gateway, run ``bin/run.sh``, open
``https://localhost:5000`` and log in. Then point this at it. A free paper
account works identically - use it.

**Unverified from the build sandbox** (needs a local gateway). Written against
IBKR's documented endpoints.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Order, OrderStatus, OrderType, Side
from .base import AccountSummary, Broker, BrokerPosition, OrderResult

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://localhost:5000/v1/api"


class IbkrBroker(Broker):
    name = "ibkr"
    supports_live = True

    def __init__(self, *, base_url: str = DEFAULT_BASE, account_id: str | None = None,
                 verify_ssl: bool = False) -> None:
        # The local gateway serves a self-signed certificate, so verification
        # is off by default. This is safe only because the endpoint is
        # localhost; never point this at a remote host with verify_ssl False.
        self.base = base_url.rstrip("/")
        self.account_id = account_id
        self.verify_ssl = verify_ssl
        self._conid_cache: dict[str, int] = {}
        self._session: Any = None

    async def _sess(self):
        import aiohttp
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False) if not self.verify_ssl else None
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=aiohttp.ClientTimeout(total=25)
            )
        return self._session

    async def _req(self, method: str, path: str, **kwargs) -> Any:
        sess = await self._sess()
        try:
            async with sess.request(method, f"{self.base}{path}", **kwargs) as resp:
                if resp.status >= 400:
                    log.error("IBKR %s %s -> %s: %s", method, path, resp.status,
                              (await resp.text())[:200])
                    return None
                return await resp.json(content_type=None)
        except Exception as exc:
            log.error(
                "IBKR %s %s failed: %s. Is the Client Portal Gateway running "
                "and authenticated at %s?", method, path, exc, self.base,
            )
            return None

    async def connect(self) -> bool:
        status = await self._req("POST", "/iserver/auth/status")
        if not status:
            log.error("cannot reach the IBKR gateway at %s", self.base)
            return False
        if not status.get("authenticated"):
            log.error(
                "IBKR gateway is running but NOT authenticated. Open %s in a "
                "browser and log in. This expires daily - a bot left unattended "
                "will stop trading until you do.",
                self.base.replace("/v1/api", ""),
            )
            return False
        accounts = await self._req("GET", "/iserver/accounts")
        ids = (accounts or {}).get("accounts") or []
        if not self.account_id and ids:
            self.account_id = ids[0]
        log.info("connected to IBKR, account %s", self.account_id)
        return bool(self.account_id)

    async def resolve_conid(self, symbol: str) -> int | None:
        if symbol in self._conid_cache:
            return self._conid_cache[symbol]
        data = await self._req("GET", "/iserver/secdef/search",
                               params={"symbol": symbol, "name": "false"})
        for item in data or []:
            conid = item.get("conid")
            if conid and str(item.get("symbol", "")).upper() == symbol.upper():
                self._conid_cache[symbol] = int(conid)
                return int(conid)
        if data and data[0].get("conid"):
            log.warning("no exact IBKR match for %s; using %s", symbol, data[0].get("symbol"))
            self._conid_cache[symbol] = int(data[0]["conid"])
            return self._conid_cache[symbol]
        return None

    async def account(self) -> AccountSummary:
        if not self.account_id:
            return AccountSummary(cash=0.0, equity=0.0)
        data = await self._req("GET", f"/portfolio/{self.account_id}/summary")
        if not data:
            return AccountSummary(cash=0.0, equity=0.0)

        def val(key: str) -> float:
            node = data.get(key) or {}
            try:
                return float(node.get("amount", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return AccountSummary(
            cash=val("availablefunds"), equity=val("netliquidation"),
            currency=(data.get("netliquidation") or {}).get("currency", "USD"),
            buying_power=val("buyingpower"),
        )

    async def positions(self) -> list[BrokerPosition]:
        if not self.account_id:
            return []
        data = await self._req("GET", f"/portfolio/{self.account_id}/positions/0")
        out: list[BrokerPosition] = []
        for item in data or []:
            out.append(BrokerPosition(
                symbol=item.get("contractDesc") or str(item.get("conid", "")),
                quantity=float(item.get("position", 0.0) or 0.0),
                avg_price=float(item.get("avgCost", 0.0) or 0.0),
                market_value=float(item.get("mktValue", 0.0) or 0.0),
                unrealised_pnl=float(item.get("unrealizedPnl", 0.0) or 0.0),
                currency=item.get("currency", "USD"),
            ))
        return out

    async def last_price(self, symbol: str) -> float | None:
        conid = await self.resolve_conid(symbol)
        if conid is None:
            return None
        # Field 31 is last price. The first snapshot call often returns an
        # empty payload while IBKR warms the subscription, so callers should
        # tolerate a None on first use.
        data = await self._req("GET", "/iserver/marketdata/snapshot",
                               params={"conids": str(conid), "fields": "31"})
        for item in data or []:
            raw = item.get("31")
            if raw is None:
                continue
            try:
                return float(str(raw).lstrip("CcHh"))
            except ValueError:
                continue
        return None

    async def place(self, order: Order) -> OrderResult:
        if not self.account_id:
            return OrderResult(False, OrderStatus.REJECTED, detail="not connected")
        conid = await self.resolve_conid(order.symbol)
        if conid is None:
            return OrderResult(False, OrderStatus.REJECTED,
                               detail=f"could not resolve {order.symbol} to a conid")

        payload = {"orders": [{
            "conid": conid,
            "orderType": "MKT" if order.order_type is OrderType.MARKET else "LMT",
            "side": "BUY" if order.side is Side.BUY else "SELL",
            "quantity": abs(order.quantity),
            "tif": "DAY",
            **({"price": order.limit_price}
               if order.order_type is OrderType.LIMIT and order.limit_price else {}),
        }]}

        data = await self._req("POST", f"/iserver/account/{self.account_id}/orders",
                               json=payload)
        if not data:
            return OrderResult(False, OrderStatus.REJECTED, detail="no response from gateway")

        # IBKR frequently answers an order with a confirmation question
        # ("this order exceeds a size threshold, proceed?") instead of a
        # result. Unanswered, the order simply never reaches the market.
        first = data[0] if isinstance(data, list) and data else {}
        if "id" in first and "message" in first:
            reply = await self._req("POST", f"/iserver/reply/{first['id']}",
                                    json={"confirmed": True})
            data = reply or data
            first = data[0] if isinstance(data, list) and data else {}

        order_id = first.get("order_id") or first.get("orderId")
        if not order_id:
            return OrderResult(False, OrderStatus.REJECTED, detail=str(data)[:200])
        return OrderResult(True, OrderStatus.PENDING,
                           broker_order_id=str(order_id), detail="order submitted")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
