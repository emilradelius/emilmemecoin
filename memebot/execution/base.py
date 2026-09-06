"""Executor interface and results."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import Position


@dataclass(slots=True)
class OrderResult:
    ok: bool
    detail: str = ""
    tx_signature: str | None = None
    filled_price_usd: float | None = None
    filled_size_sol: float = 0.0
    tokens: float = 0.0


class Executor(ABC):
    """Places orders. Implementations: paper (simulated) and live (real)."""

    mode: str = "base"

    @abstractmethod
    async def buy(self, mint: str, size_sol: float, *,
                  price_usd: float | None = None) -> OrderResult: ...

    @abstractmethod
    async def sell(self, position: Position, fraction: float, *,
                   price_usd: float | None = None) -> OrderResult: ...

    async def wallet_balance_sol(self) -> float | None:
        return None

    async def close(self) -> None:
        return None
