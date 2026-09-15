"""Paper broker: full simulation, no account needed anywhere.

This exists so the whole system can be developed, tested and run end-to-end
before you have opened an account with anybody. It applies the same cost model
as the backtester, so paper results and backtest results are directly
comparable - if they diverge, something in the live path is wrong, and that is
exactly what you want to find out here rather than later.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..costs import CostModel
from ..models import Order, OrderStatus, PositionState, Side
from .base import AccountSummary, Broker, BrokerPosition, OrderResult

log = logging.getLogger(__name__)


class PaperBroker(Broker):
    name = "paper"
    supports_live = False

    def __init__(self, costs: CostModel, *, starting_cash: float = 100_000.0,
                 currency: str = "SEK", needs_fx: bool = False) -> None:
        self.costs = costs
        self.cash = starting_cash
        self.currency = currency
        self.needs_fx = needs_fx
        self._positions: dict[str, PositionState] = {}
        self._prices: dict[str, float] = {}
        self._orders = 0

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    async def connect(self) -> bool:
        return True

    async def last_price(self, symbol: str) -> float | None:
        return self._prices.get(symbol)

    async def account(self) -> AccountSummary:
        held = sum(
            p.market_value(self._prices.get(s, p.avg_price))
            for s, p in self._positions.items()
        )
        return AccountSummary(
            cash=self.cash, equity=self.cash + held,
            currency=self.currency, buying_power=self.cash,
        )

    async def positions(self) -> list[BrokerPosition]:
        out: list[BrokerPosition] = []
        for sym, p in self._positions.items():
            if not p.is_open:
                continue
            price = self._prices.get(sym, p.avg_price)
            out.append(BrokerPosition(
                symbol=sym, quantity=p.quantity, avg_price=p.avg_price,
                market_value=p.market_value(price),
                unrealised_pnl=p.unrealised_pnl(price), currency=self.currency,
            ))
        return out

    async def place(self, order: Order) -> OrderResult:
        price = self._prices.get(order.symbol)
        if price is None or price <= 0:
            return OrderResult(False, OrderStatus.REJECTED,
                               detail=f"no price known for {order.symbol}")
        if order.quantity <= 0:
            return OrderResult(False, OrderStatus.REJECTED, detail="non-positive quantity")

        value = order.quantity * price
        cost = self.costs.total(value, needs_conversion=self.needs_fx)
        pos = self._positions.setdefault(order.symbol, PositionState(order.symbol))

        if order.side is Side.BUY:
            if value + cost > self.cash:
                return OrderResult(False, OrderStatus.REJECTED,
                                   detail=f"insufficient cash: need {value + cost:,.0f}, "
                                          f"have {self.cash:,.0f}")
            new_qty = pos.quantity + order.quantity
            pos.avg_price = (
                (pos.avg_price * pos.quantity + price * order.quantity) / new_qty
                if new_qty else price
            )
            pos.quantity = new_qty
            self.cash -= value + cost
        else:
            if order.quantity > pos.quantity + 1e-9:
                return OrderResult(False, OrderStatus.REJECTED,
                                   detail=f"cannot sell {order.quantity}, hold {pos.quantity}")
            pos.quantity -= order.quantity
            self.cash += value - cost
            if not pos.is_open:
                pos.quantity = 0.0
                pos.avg_price = 0.0

        self._orders += 1
        log.info("[paper] %s %.4f %s at %.4f (costs %.2f)",
                 order.side.value.upper(), order.quantity, order.symbol, price, cost)
        return OrderResult(
            True, OrderStatus.FILLED, broker_order_id=f"paper-{self._orders}",
            filled_quantity=order.quantity, avg_fill_price=price,
            detail=f"simulated fill, costs {cost:,.2f}",
        )
