"""Saxo Bank OpenAPI adapter.

The best fit for a Swedish retail algo account: OpenAPI is a documented,
supported, first-party REST + streaming API covering 71,000+ instruments, and
Saxo runs a **free simulation environment** with the identical API surface.
That matters more than it sounds - you can develop and run the entire bot
against SIM with a 24-hour developer token and no funded account.

Two things about Saxo that surprise people:

* **Instruments are addressed by UIC, not ticker.** ``VOLV-B.ST`` means
  nothing to the API; you must resolve it to a numeric UIC first. Resolution
  is cached here because it is stable and rate limits are real.
* **Tokens are short-lived, and this decides whether a multi-day run is
  possible at all.** The copy-paste SIM developer token lasts 24 hours, so a
  seven-day trial on one dies after day one. The fix is to register an app on
  Saxo's developer portal and use the OAuth2 refresh flow: 20-minute access
  tokens, renewed automatically from a long-lived refresh token. Pass
  ``refresh_token``, ``client_id`` and ``client_secret`` and this class handles
  renewal itself.

  One trap in that flow: **Saxo rotates the refresh token on every use.** The
  old one is dead immediately, so a process that does not persist the new one
  breaks its own credential chain the first time it restarts. Give
  ``token_store`` a path and it is written to disk on every refresh.

**Unverified from the build sandbox** (no credentials, and its proxy blocks
outbound calls). Written against Saxo's documented shapes. Run
``python -m brokerbot.cli broker --check saxo`` against SIM before trusting it.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from ..models import Order, OrderStatus, OrderType, Side
from .base import AccountSummary, Broker, BrokerPosition, OrderResult

log = logging.getLogger(__name__)

SIM_BASE = "https://gateway.saxobank.com/sim/openapi"
LIVE_BASE = "https://gateway.saxobank.com/openapi"
SIM_AUTH = "https://sim.logonvalidation.net/token"
LIVE_AUTH = "https://live.logonvalidation.net/token"


class SaxoBroker(Broker):
    name = "saxo"
    supports_live = True

    def __init__(self, token: str | None = None, *, simulation: bool = True,
                 account_key: str | None = None, asset_type: str = "Stock",
                 refresh_token: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None,
                 token_store: str | Path | None = None) -> None:
        self.base = SIM_BASE if simulation else LIVE_BASE
        self.auth_base = SIM_AUTH if simulation else LIVE_AUTH
        self.simulation = simulation
        self.account_key = account_key
        self.asset_type = asset_type

        self.client_id = client_id
        self.client_secret = client_secret
        self.token_store = Path(token_store) if token_store else None
        self._refresh_token = refresh_token
        self.token = token
        self._expires_at: float = 0.0
        self._load_tokens()

        if not self.token and not self._refresh_token:
            raise ValueError(
                "Saxo needs either a 24h developer token, or a refresh_token "
                "with client_id/client_secret for an unattended multi-day run"
            )
        if self.token and not self._refresh_token:
            log.warning(
                "using a static Saxo token - it expires within 24h and the run "
                "will stop. Use the OAuth2 refresh flow for anything longer."
            )

        self._uic_cache: dict[str, int] = {}
        self._session: Any = None

        if not simulation:
            log.warning(
                "Saxo LIVE mode: orders will use real money. Confirm you meant "
                "this - SIM uses the identical API and is free."
            )

    # --- token handling ---------------------------------------------------
    def _load_tokens(self) -> None:
        if self.token_store is None or not self.token_store.exists():
            return
        try:
            data = json.loads(self.token_store.read_text())
        except (OSError, ValueError):
            log.warning("could not read Saxo token store; using supplied values")
            return
        # A stored refresh token is newer than anything passed in, because
        # Saxo invalidates the previous one on every rotation.
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        self.token = data.get("access_token") or self.token
        self._expires_at = float(data.get("expires_at", 0.0))

    def _save_tokens(self) -> None:
        if self.token_store is None:
            return
        try:
            self.token_store.parent.mkdir(parents=True, exist_ok=True)
            self.token_store.write_text(json.dumps({
                "access_token": self.token,
                "refresh_token": self._refresh_token,
                "expires_at": self._expires_at,
            }))
            # Credentials: owner-readable only.
            self.token_store.chmod(0o600)
        except OSError as exc:
            log.warning("could not persist Saxo tokens: %s", exc)

    @property
    def can_refresh(self) -> bool:
        return bool(self._refresh_token and self.client_id and self.client_secret)

    async def ensure_token(self) -> bool:
        """Refresh the access token when it is close to expiring."""
        if self.token and time.time() < self._expires_at - 120:
            return True
        if not self.can_refresh:
            # A static token cannot be renewed; report whether one exists so
            # the caller can surface an expiry rather than guessing.
            return bool(self.token)
        return await self._refresh()

    async def _refresh(self) -> bool:
        import aiohttp
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            ) as sess:
                async with sess.post(
                    self.auth_base,
                    data={"grant_type": "refresh_token",
                          "refresh_token": self._refresh_token},
                    headers={"Authorization": f"Basic {basic}",
                             "Content-Type": "application/x-www-form-urlencoded"},
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        log.error("Saxo token refresh failed (%s): %s",
                                  resp.status, str(body)[:200])
                        return False
        except Exception as exc:
            log.error("Saxo token refresh request failed: %s", exc)
            return False

        self.token = body.get("access_token")
        # Rotation: the new refresh token replaces the old one, which is now
        # dead. Losing this write breaks the chain on the next restart.
        self._refresh_token = body.get("refresh_token") or self._refresh_token
        self._expires_at = time.time() + float(body.get("expires_in", 1200))
        self._save_tokens()
        if self._session is not None and not self._session.closed:
            # Force a new session so the fresh token is used.
            await self._session.close()
            self._session = None
        log.info("Saxo access token refreshed, valid for %.0f minutes",
                 float(body.get("expires_in", 1200)) / 60)
        return bool(self.token)

    async def _sess(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=25),
            )
        return self._session

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        sess = await self._sess()
        try:
            async with sess.get(f"{self.base}{path}", params=params) as resp:
                if resp.status == 401:
                    raise RuntimeError(
                        "Saxo returned 401 - the token has expired. SIM developer "
                        "tokens last 24h; live OAuth2 tokens last 20 minutes."
                    )
                if resp.status >= 400:
                    log.error("Saxo GET %s -> %s: %s", path, resp.status,
                              (await resp.text())[:200])
                    return None
                return await resp.json(content_type=None)
        except Exception as exc:
            log.error("Saxo GET %s failed: %s", path, exc)
            return None

    async def connect(self) -> bool:
        if not await self.ensure_token():
            log.error(
                "no valid Saxo access token. A static developer token lasts "
                "24h; configure the OAuth2 refresh flow for a longer run."
            )
            return False
        data = await self._get("/port/v1/accounts/me")
        if not data:
            return False
        accounts = data.get("Data") or []
        if not accounts:
            log.error("Saxo returned no accounts for this token")
            return False
        if not self.account_key:
            self.account_key = accounts[0].get("AccountKey")
        log.info("connected to Saxo %s, account %s",
                 "SIM" if self.simulation else "LIVE", self.account_key)
        return bool(self.account_key)

    async def resolve_uic(self, symbol: str) -> int | None:
        """Map a ticker to Saxo's numeric instrument id."""
        if symbol in self._uic_cache:
            return self._uic_cache[symbol]
        data = await self._get("/ref/v1/instruments", {
            "Keywords": symbol, "AssetTypes": self.asset_type,
        })
        for item in (data or {}).get("Data", []):
            uic = item.get("Identifier")
            if uic is None:
                continue
            # Prefer an exact symbol match; keyword search is fuzzy and the
            # first result is often a different listing of a similar name.
            if str(item.get("Symbol", "")).upper() == symbol.upper():
                self._uic_cache[symbol] = int(uic)
                return int(uic)
        items = (data or {}).get("Data", [])
        if items and items[0].get("Identifier") is not None:
            log.warning("no exact Saxo match for %s; using %s",
                        symbol, items[0].get("Symbol"))
            self._uic_cache[symbol] = int(items[0]["Identifier"])
            return self._uic_cache[symbol]
        return None

    async def account(self) -> AccountSummary:
        data = await self._get("/port/v1/balances/me")
        if not data:
            return AccountSummary(cash=0.0, equity=0.0)
        return AccountSummary(
            cash=float(data.get("CashBalance", 0.0)),
            equity=float(data.get("TotalValue", 0.0)),
            currency=data.get("Currency", "SEK"),
            buying_power=float(data.get("MarginAvailableForTrading", 0.0) or 0.0),
        )

    async def positions(self) -> list[BrokerPosition]:
        data = await self._get("/port/v1/netpositions/me")
        out: list[BrokerPosition] = []
        for item in (data or {}).get("Data", []):
            base = item.get("NetPositionBase", {}) or {}
            view = item.get("NetPositionView", {}) or {}
            out.append(BrokerPosition(
                symbol=str(base.get("Symbol") or base.get("Uic", "")),
                quantity=float(base.get("Amount", 0.0) or 0.0),
                avg_price=float(base.get("AverageOpenPrice", 0.0) or 0.0),
                market_value=float(view.get("MarketValue", 0.0) or 0.0),
                unrealised_pnl=float(view.get("ProfitLossOnTrade", 0.0) or 0.0),
                currency=base.get("Currency", "SEK"),
            ))
        return out

    async def last_price(self, symbol: str) -> float | None:
        uic = await self.resolve_uic(symbol)
        if uic is None:
            return None
        data = await self._get("/trade/v1/infoprices", {
            "Uic": uic, "AssetType": self.asset_type,
        })
        quote = (data or {}).get("Quote") or {}
        for key in ("Mid", "Ask", "Bid", "LastTraded"):
            if quote.get(key):
                return float(quote[key])
        return None

    async def place(self, order: Order) -> OrderResult:
        if not self.account_key:
            return OrderResult(False, OrderStatus.REJECTED, detail="not connected")
        uic = await self.resolve_uic(order.symbol)
        if uic is None:
            return OrderResult(False, OrderStatus.REJECTED,
                               detail=f"could not resolve {order.symbol} to a Saxo UIC")

        payload: dict[str, Any] = {
            "AccountKey": self.account_key,
            "Uic": uic,
            "AssetType": self.asset_type,
            "Amount": abs(order.quantity),
            "BuySell": "Buy" if order.side is Side.BUY else "Sell",
            "OrderType": "Market" if order.order_type is OrderType.MARKET else "Limit",
            "OrderDuration": {"DurationType": "DayOrder"},
        }
        if order.order_type is OrderType.LIMIT and order.limit_price:
            payload["OrderPrice"] = order.limit_price

        sess = await self._sess()
        try:
            async with sess.post(f"{self.base}/trade/v2/orders", json=payload) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    return OrderResult(
                        False, OrderStatus.REJECTED,
                        detail=f"Saxo {resp.status}: {str(body)[:200]}",
                    )
        except Exception as exc:
            return OrderResult(False, OrderStatus.REJECTED, detail=f"request failed: {exc}")

        return OrderResult(
            True, OrderStatus.PENDING,
            broker_order_id=str((body or {}).get("OrderId", "")),
            detail="order submitted",
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
