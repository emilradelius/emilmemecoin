"""Simulated execution.

Paper mode is not a toy - it is how you find out whether the filters in
``config.yaml`` actually make money before any is at risk. It does full PnL
accounting against real observed prices, so after a few weeks the position
history is a genuine backtest of the live signal path (rather than of a
historical replay, which would quietly lie to you about latency and about
which signals you would really have seen).

It deliberately models two costs that a naive simulator ignores, because
ignoring them is what makes paper results look far better than live ones:

* **Slippage** on entry and exit, scaled by position size against pool depth.
* **Fees** - priority fee plus the DEX/trading fee.
"""

from __future__ import annotations

import logging
import time

from ..config import Config
from ..enrich.dexscreener import DexScreener
from ..models import Position
from .base import Executor, OrderResult

log = logging.getLogger(__name__)


class PaperExecutor(Executor):
    mode = "paper"

    def __init__(self, cfg: Config, dex: DexScreener, *,
                 starting_balance_sol: float = 10.0) -> None:
        e = cfg.section("execution")
        self.slippage_bps = e.get("slippage_bps", 1000)
        self.priority_fee = e.get("priority_fee_sol", 0.0005)
        self.dex = dex
        self.balance_sol = starting_balance_sol
        # pump.fun / Raydium style taker fee.
        self.trading_fee_pct = 0.01

    async def wallet_balance_sol(self) -> float | None:
        return self.balance_sol

    def _slippage_pct(self, size_sol: float, liquidity_usd: float | None,
                      sol_price: float) -> float:
        """Bigger orders into thinner pools fill worse. Roughly linear in the
        fraction of the pool you are taking, which is close enough for a
        constant-product AMM at the sizes this bot trades."""
        if not liquidity_usd or liquidity_usd <= 0:
            return self.slippage_bps / 10000.0
        order_usd = size_sol * sol_price
        impact = order_usd / liquidity_usd
        return min(self.slippage_bps / 10000.0, max(0.002, impact * 2.0))

    async def buy(self, mint: str, size_sol: float, *,
                  price_usd: float | None = None) -> OrderResult:
        market = await self.dex.get(mint)
        price = price_usd or (market.price_usd if market else None)
        if not price:
            return OrderResult(False, "no price available for simulated fill")
        if size_sol > self.balance_sol:
            return OrderResult(False, f"insufficient paper balance ({self.balance_sol:.3f} SOL)")

        sol_price = 200.0
        wsol = await self.dex.get("So11111111111111111111111111111111111111112")
        if wsol and wsol.price_usd:
            sol_price = wsol.price_usd

        slip = self._slippage_pct(size_sol, market.liquidity_usd if market else None, sol_price)
        fill_price = price * (1 + slip)
        spent_sol = size_sol + self.priority_fee
        tokens = (size_sol * sol_price * (1 - self.trading_fee_pct)) / fill_price

        self.balance_sol -= spent_sol
        log.info(
            "[paper] BUY %s: %.4f SOL at $%.10f (slip %.2f%%) -> %.0f tokens",
            mint[:8], size_sol, fill_price, slip * 100, tokens,
        )
        return OrderResult(
            True, f"simulated fill, {slip * 100:.2f}% slippage",
            tx_signature=f"paper-{int(time.time())}",
            filled_price_usd=fill_price, filled_size_sol=size_sol, tokens=tokens,
        )

    async def sell(self, position: Position, fraction: float, *,
                   price_usd: float | None = None) -> OrderResult:
        market = await self.dex.get(position.token_mint)
        price = price_usd or (market.price_usd if market else None)
        if not price:
            return OrderResult(False, "no price available for simulated fill")

        sol_price = 200.0
        wsol = await self.dex.get("So11111111111111111111111111111111111111112")
        if wsol and wsol.price_usd:
            sol_price = wsol.price_usd

        tokens_out = position.tokens_held * fraction
        gross_usd = tokens_out * price
        slip = self._slippage_pct(gross_usd / sol_price, market.liquidity_usd if market else None, sol_price)
        fill_price = price * (1 - slip)
        net_usd = tokens_out * fill_price * (1 - self.trading_fee_pct)
        proceeds_sol = net_usd / sol_price - self.priority_fee

        self.balance_sol += proceeds_sol
        log.info(
            "[paper] SELL %.0f%% of %s at $%.10f -> %.4f SOL",
            fraction * 100, position.token_mint[:8], fill_price, proceeds_sol,
        )
        return OrderResult(
            True, f"simulated fill, {slip * 100:.2f}% slippage",
            tx_signature=f"paper-{int(time.time())}",
            filled_price_usd=fill_price, filled_size_sol=proceeds_sol, tokens=tokens_out,
        )
