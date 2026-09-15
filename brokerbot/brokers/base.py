"""Broker interface.

Deliberately small. Every broker here speaks a different protocol - Saxo is
OAuth2 + REST, IBKR is a local gateway you must keep authenticated by hand,
eToro is API-key REST - and the strategy layer should never learn any of that.

The contract is also intentionally *read-heavy*: positions and balances come
from the broker, never from local bookkeeping. Local state drifts. If a fill
partially executed, or you placed a manual trade in the app, or an order was
rejected while your process was restarting, the broker knows and you do not.
A bot trading on its own stale idea of what it owns is how small bugs turn
into large ones.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import Order, OrderStatus


@dataclass(slots=True)
class BrokerPosition:
    symbol: str
    quantity: float
    avg_price: float
    market_value: float = 0.0
    unrealised_pnl: float = 0.0
    currency: str = "USD"


@dataclass(slots=True)
class AccountSummary:
    cash: float
    equity: float
    currency: str = "SEK"
    buying_power: float = 0.0


@dataclass(slots=True)
class OrderResult:
    ok: bool
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    detail: str = ""


class Broker(ABC):
    name: str = "broker"
    supports_live: bool = False

    @abstractmethod
    async def connect(self) -> bool: ...

    @abstractmethod
    async def account(self) -> AccountSummary: ...

    @abstractmethod
    async def positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def place(self, order: Order) -> OrderResult: ...

    @abstractmethod
    async def last_price(self, symbol: str) -> float | None: ...

    async def cancel(self, broker_order_id: str) -> bool:
        return False

    async def close(self) -> None:
        return None
