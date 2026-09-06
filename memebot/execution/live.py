"""Live execution on Solana, via PumpPortal's local (self-signing) API.

**Non-custodial by construction.** PumpPortal offers two trading APIs: a
"Lightning" one where they hold the key and sign for you, and a "Local" one
that returns an *unsigned* serialized transaction which you sign yourself.
This module uses Local only. Your private key never leaves the machine, and a
compromise at the API provider cannot move your funds.

Even so, live mode means a hot wallet on a server. The whole design assumes
that key will eventually leak:

* Use a **burner wallet**, funded with only what you are willing to lose.
* Position and loss caps in ``guardrails.py`` bound a bad day.
* ``execution.live_mode_armed`` must be set in the config *file* - it cannot
  be enabled from Telegram, so a compromised chat cannot turn on trading.

``solders`` is imported lazily so the base install does not need it.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import aiohttp

from ..config import Config
from ..models import Position
from .base import Executor, OrderResult

log = logging.getLogger(__name__)

TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"


class LiveExecutor(Executor):
    mode = "live"

    def __init__(self, cfg: Config, dex, *, private_key: str | None = None,
                 rpc_url: str | None = None) -> None:
        e = cfg.section("execution")
        self.slippage_pct = e.get("slippage_bps", 1000) / 100.0
        self.priority_fee = e.get("priority_fee_sol", 0.0005)
        self.max_price_impact = e.get("max_price_impact_pct", 8.0)
        self.dex = dex
        self.rpc_url = rpc_url or os.getenv("SOLANA_RPC_URL") or DEFAULT_RPC

        self._keypair = None
        self._pubkey: str | None = None
        self._private_key = private_key or os.getenv("TRADING_WALLET_PRIVATE_KEY")
        self._session: aiohttp.ClientSession | None = None

    # --- key handling -----------------------------------------------------
    def _load_keypair(self):
        if self._keypair is not None:
            return self._keypair
        if not self._private_key:
            raise RuntimeError(
                "TRADING_WALLET_PRIVATE_KEY is not set - cannot trade live"
            )
        try:
            from solders.keypair import Keypair  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "live trading needs the 'solders' package: "
                "pip install -r requirements-live.txt"
            ) from exc

        key = self._private_key.strip()
        try:
            if key.startswith("["):
                import json
                self._keypair = Keypair.from_bytes(bytes(json.loads(key)))
            else:
                import base58
                self._keypair = Keypair.from_bytes(base58.b58decode(key))
        except Exception as exc:
            raise RuntimeError(f"could not parse TRADING_WALLET_PRIVATE_KEY: {exc}") from exc

        self._pubkey = str(self._keypair.pubkey())
        log.info("live executor loaded wallet %s", self._pubkey)
        return self._keypair

    @property
    def public_key(self) -> str:
        self._load_keypair()
        assert self._pubkey
        return self._pubkey

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    # --- order construction ----------------------------------------------
    async def _build_and_send(self, payload: dict[str, Any]) -> OrderResult:
        session = await self._sess()
        try:
            async with session.post(TRADE_LOCAL_URL, json=payload) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    return OrderResult(False, f"trade-local HTTP {resp.status}: {body}")
                tx_bytes = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as exc:
            return OrderResult(False, f"trade-local request failed: {exc}")

        if not tx_bytes:
            return OrderResult(False, "trade-local returned an empty transaction")

        try:
            from solders.transaction import VersionedTransaction  # type: ignore[import-not-found]
        except ImportError as exc:
            return OrderResult(False, f"solders not installed: {exc}")

        keypair = self._load_keypair()
        try:
            unsigned = VersionedTransaction.from_bytes(tx_bytes)
            signed = VersionedTransaction(unsigned.message, [keypair])
            raw = bytes(signed)
        except Exception as exc:
            return OrderResult(False, f"could not sign transaction: {exc}")

        return await self._send_raw(raw)

    async def _send_raw(self, raw: bytes) -> OrderResult:
        session = await self._sess()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(raw).decode(),
                {"encoding": "base64", "skipPreflight": False,
                 "preflightCommitment": "confirmed", "maxRetries": 3},
            ],
        }
        try:
            async with session.post(self.rpc_url, json=body) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            return OrderResult(False, f"RPC send failed: {exc}")

        if "error" in data:
            return OrderResult(False, f"RPC error: {data['error']}")
        sig = data.get("result")
        if not sig:
            return OrderResult(False, f"unexpected RPC response: {str(data)[:200]}")
        log.info("live tx submitted: %s", sig)
        return OrderResult(True, "submitted", tx_signature=sig)

    # --- orders -----------------------------------------------------------
    async def buy(self, mint: str, size_sol: float, *,
                  price_usd: float | None = None) -> OrderResult:
        market = await self.dex.get(mint)
        price = price_usd or (market.price_usd if market else None)

        # Refuse to enter something whose real depth is far below what the
        # alert implied. This is the last line of defence against a token
        # whose advertised liquidity is fake.
        if market and market.liquidity_usd:
            sol_price = 200.0
            wsol = await self.dex.get("So11111111111111111111111111111111111111112")
            if wsol and wsol.price_usd:
                sol_price = wsol.price_usd
            impact_pct = (size_sol * sol_price) / market.liquidity_usd * 100
            if impact_pct > self.max_price_impact:
                return OrderResult(
                    False,
                    f"price impact {impact_pct:.1f}% exceeds "
                    f"{self.max_price_impact}% limit",
                )

        result = await self._build_and_send({
            "publicKey": self.public_key,
            "action": "buy",
            "mint": mint,
            "amount": size_sol,
            "denominatedInSol": "true",
            "slippage": self.slippage_pct,
            "priorityFee": self.priority_fee,
            "pool": "auto",
        })
        if result.ok:
            result.filled_price_usd = price
            result.filled_size_sol = size_sol
        return result

    async def sell(self, position: Position, fraction: float, *,
                   price_usd: float | None = None) -> OrderResult:
        market = await self.dex.get(position.token_mint)
        price = price_usd or (market.price_usd if market else None)

        # Sell by percentage so we never try to sell more than we hold - the
        # on-chain balance is authoritative, our tracked figure is not.
        amount: Any = f"{int(round(fraction * 100))}%"

        result = await self._build_and_send({
            "publicKey": self.public_key,
            "action": "sell",
            "mint": position.token_mint,
            "amount": amount,
            "denominatedInSol": "false",
            "slippage": self.slippage_pct,
            "priorityFee": self.priority_fee,
            "pool": "auto",
        })
        if result.ok:
            result.filled_price_usd = price
            result.tokens = position.tokens_held * fraction
        return result

    async def wallet_balance_sol(self) -> float | None:
        session = await self._sess()
        try:
            async with session.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                "params": [self.public_key],
            }) as resp:
                data = await resp.json(content_type=None)
            lamports = (data.get("result") or {}).get("value")
            return lamports / 1e9 if lamports is not None else None
        except Exception:
            log.exception("could not fetch wallet balance")
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
