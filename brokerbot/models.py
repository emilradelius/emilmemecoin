"""Core types for broker-connected trading.

Separate package from ``memebot`` on purpose. That bot trades tokens minutes
old on decentralised exchanges; this one trades listed instruments through a
regulated broker. The venues, the data, the cost structure and the realistic
edge are all different, and merging them would produce a system that is wrong
about both.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


def _uid() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(slots=True, frozen=True)
class Bar:
    """One OHLCV period for one instrument.

    Frozen because a backtest that mutates its own history is the easiest way
    to introduce look-ahead bias without noticing.
    """

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def day(self) -> date:
        return self.ts.date()

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(slots=True)
class Order:
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    ts: datetime | None = None
    reason: str = ""
    id: str = field(default_factory=_uid)


@dataclass(slots=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    ts: datetime
    commission: float = 0.0
    slippage_cost: float = 0.0
    fx_cost: float = 0.0

    @property
    def gross_value(self) -> float:
        return self.quantity * self.price

    @property
    def total_costs(self) -> float:
        return self.commission + self.slippage_cost + self.fx_cost

    @property
    def cash_delta(self) -> float:
        """Signed effect on cash, costs included. Costs always reduce cash."""
        signed = -self.gross_value if self.side is Side.BUY else self.gross_value
        return signed - self.total_costs


@dataclass(slots=True)
class PositionState:
    symbol: str
    quantity: float = 0.0
    avg_price: float = 0.0

    @property
    def is_open(self) -> bool:
        return abs(self.quantity) > 1e-9

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealised_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.quantity


@dataclass(slots=True)
class ClosedTrade:
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    quantity: float
    entry_price: float
    exit_price: float
    costs: float = 0.0

    @property
    def gross_pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.costs

    @property
    def return_pct(self) -> float:
        cost_basis = self.entry_price * abs(self.quantity)
        return self.net_pnl / cost_basis if cost_basis else 0.0

    @property
    def hold_days(self) -> float:
        return (self.exit_ts - self.entry_ts).total_seconds() / 86400.0

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0


@dataclass(slots=True)
class EquityPoint:
    ts: datetime
    cash: float
    positions_value: float

    @property
    def total(self) -> float:
        return self.cash + self.positions_value
