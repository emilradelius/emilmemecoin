"""Pump.fun signals, via the PumpPortal websocket.

An important framing note: pump.fun's iPhone app presents as a social feed,
but a pump.fun profile *is* a Solana wallet. Everything the feed shows you -
who bought what, at what price, how much - is on-chain data rendered
socially. So the reliable way to follow the traders you see in that app is
not to scrape the app, it is to watch their wallets directly. That is what
this source does, and it is both more complete (nothing is missed) and more
timely (no feed delay) than the app itself.

We subscribe to ``subscribeAccountTrade`` for the specific wallets the
scorer has promoted, never the global firehose - the global feed is tens of
trades per second and is almost entirely bots.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import websockets

from ..config import Config
from ..models import Side, Signal, Source
from .base import SignalSource

log = logging.getLogger(__name__)


def _first(d: dict[str, Any], *names: str, default: Any = None) -> Any:
    """Read the first present key. PumpPortal's field names have changed
    across revisions and differ between pools, so every read is tolerant."""
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


class PumpFunSource(SignalSource):
    name = "pumpfun"

    def __init__(
        self,
        cfg: Config,
        *,
        sol_price_fn=None,
        actor_score_fn=None,
    ) -> None:
        super().__init__()
        s = cfg.section("sources").get("pumpfun", {})
        self.enabled = s.get("enabled", True)
        self.ws_url = s.get("ws_url", "wss://pumpportal.fun/api/data")
        self.reconnect_base = s.get("reconnect_base_seconds", 2)
        self.reconnect_max = s.get("reconnect_max_seconds", 120)
        self.max_wallets = s.get("max_tracked_wallets", 150)

        self._wallets: set[str] = set()
        self._ws: Any = None
        self._sol_price_fn = sol_price_fn      # async () -> float | None
        self._actor_score_fn = actor_score_fn  # (wallet) -> float
        self._pending_resubscribe = asyncio.Event()

        # Graduations are free to subscribe to and are the outcome label the
        # wallet-discovery module needs.
        self.on_migration = None
        self.on_new_token = None

    # --- tracked wallet management ---------------------------------------
    def set_wallets(self, wallets: list[str]) -> None:
        """Replace the tracked set. Safe to call while running; the socket
        loop re-subscribes on the next tick."""
        new = set(wallets[: self.max_wallets])
        if new != self._wallets:
            added, removed = new - self._wallets, self._wallets - new
            log.info(
                "pump.fun tracked wallets: %d (+%d/-%d)",
                len(new), len(added), len(removed),
            )
            self._wallets = new
            self._pending_resubscribe.set()

    @property
    def tracked_count(self) -> int:
        return len(self._wallets)

    # --- main loop --------------------------------------------------------
    async def run(self, out: asyncio.Queue[Signal]) -> None:
        if not self.enabled:
            log.info("pump.fun source disabled in config")
            return

        backoff = float(self.reconnect_base)
        while True:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=20, max_queue=512
                ) as ws:
                    self._ws = ws
                    backoff = float(self.reconnect_base)
                    await self._subscribe(ws)
                    log.info(
                        "pump.fun websocket connected, tracking %d wallets",
                        len(self._wallets),
                    )
                    resub_task = asyncio.create_task(self._resubscribe_loop(ws))
                    try:
                        async for raw in ws:
                            await self._handle(raw, out)
                    finally:
                        resub_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await resub_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "pump.fun websocket dropped (%s); reconnecting in %.0fs",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, float(self.reconnect_max))
            finally:
                self._ws = None

    async def _subscribe(self, ws: Any) -> None:
        # Free streams: token creation and bonding-curve graduations. The
        # latter is the outcome signal wallet discovery is built on.
        await ws.send(json.dumps({"method": "subscribeMigration"}))
        if self._wallets:
            await ws.send(
                json.dumps(
                    {"method": "subscribeAccountTrade",
                     "keys": sorted(self._wallets)}
                )
            )

    async def _resubscribe_loop(self, ws: Any) -> None:
        """Apply tracked-wallet changes without dropping the connection."""
        while True:
            await self._pending_resubscribe.wait()
            self._pending_resubscribe.clear()
            try:
                if self._wallets:
                    await ws.send(
                        json.dumps(
                            {"method": "subscribeAccountTrade",
                             "keys": sorted(self._wallets)}
                        )
                    )
                    log.info("re-subscribed to %d wallets", len(self._wallets))
            except Exception:
                log.exception("re-subscribe failed; the reconnect loop will retry")
                return

    # --- message handling -------------------------------------------------
    async def _handle(self, raw: str | bytes, out: asyncio.Queue[Signal]) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return

        # Subscription acknowledgements and errors.
        if "message" in msg and "mint" not in msg:
            log.debug("pump.fun control message: %s", msg.get("message"))
            return

        if _first(msg, "txType", "tx_type") in {"create", "created"}:
            if self.on_new_token:
                await self._safe_cb(self.on_new_token, msg)
            return

        if "migration" in str(msg.get("txType", "")).lower() or msg.get("pool") == "raydium-migration":
            if self.on_migration:
                await self._safe_cb(self.on_migration, msg)
            return

        sig = await self.parse_trade(msg)
        if sig is not None:
            await self.emit(out, sig)

    async def _safe_cb(self, cb, msg: dict[str, Any]) -> None:
        try:
            res = cb(msg)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            log.exception("pump.fun callback failed")

    async def parse_trade(self, msg: dict[str, Any]) -> Signal | None:
        """Convert one PumpPortal trade message into a Signal.

        Kept separate from the socket loop so it can be unit-tested against
        recorded payloads without a network connection - see
        ``memebot/tools/probe_schemas.py`` for capturing real ones.
        """
        mint = _first(msg, "mint", "tokenMint", "token_mint")
        wallet = _first(msg, "traderPublicKey", "trader_public_key", "account", "wallet")
        tx_type = str(_first(msg, "txType", "tx_type", default="")).lower()
        if not mint or not wallet or tx_type not in {"buy", "sell"}:
            return None

        sol_amount = _first(msg, "solAmount", "sol_amount", "solInPool", default=None)
        try:
            sol_amount = float(sol_amount) if sol_amount is not None else None
        except (TypeError, ValueError):
            sol_amount = None

        size_usd = None
        price_usd = None
        if sol_amount is not None and self._sol_price_fn:
            sol_price = await self._sol_price_fn()
            if sol_price:
                size_usd = sol_amount * sol_price
                token_amount = _first(msg, "tokenAmount", "token_amount")
                try:
                    token_amount = float(token_amount) if token_amount else None
                except (TypeError, ValueError):
                    token_amount = None
                if token_amount:
                    price_usd = size_usd / token_amount

        actor_score = 0.5
        if self._actor_score_fn:
            try:
                actor_score = self._actor_score_fn(wallet)
            except Exception:
                log.debug("actor score lookup failed for %s", wallet[:8])

        return Signal(
            source=Source.PUMPFUN,
            actor_id=wallet,
            token_mint=mint,
            token_symbol=_first(msg, "symbol", "tokenSymbol"),
            side=Side.BUY if tx_type == "buy" else Side.SELL,
            size_usd=size_usd,
            price_usd=price_usd,
            actor_score=actor_score,
            confidence=1.0,   # on-chain: this definitely happened
            ts=time.time(),
            url=f"https://pump.fun/coin/{mint}",
            raw=msg,
        )
