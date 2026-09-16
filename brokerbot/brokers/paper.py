"""Paper broker: full simulation, no account needed anywhere.

This exists so the whole system can be developed, tested and run end-to-end
before you have opened an account with anybody. It applies the same cost model
as the backtester, so paper results and backtest results are directly
comparable - if they diverge, something in the live path is wrong, and that is
exactly what you want to find out here rather than later.

Fills need a price, and a simulated broker has no market to ask. Prices
therefore come from one of two places: pushed in with :meth:`set_price` (what
the tests do), or pulled from an optional ``quotes`` source - see
:mod:`brokerbot.data.yahoo`. Without either, every order is rejected for want
of a price, which is the honest outcome: a paper fill at an invented price
teaches you nothing you would not have learned by guessing.

``supports_live`` stays ``False``. A free quote feed is good enough to
rehearse the machinery and nowhere near good enough to size a real order.
"""

from __future__ import annotations

import logging
from typing import Any

from ..costs import CostModel
from ..models import Order, OrderStatus, PositionState, Side
from .base import AccountSummary, Broker, BrokerPosition, OrderResult

log = logging.getLogger(__name__)


class PaperBroker(Broker):
    name = "paper"
    supports_live = False

    def __init__(self, costs: CostModel, *, starting_cash: float = 100_000.0,
                 currency: str = "SEK", needs_fx: bool = False,
                 quotes: Any = None) -> None:
        self.costs = costs
        self.cash = starting_cash
        self.currency = currency
        self.needs_fx = needs_fx
        self.quotes = quotes
        self._positions: dict[str, PositionState] = {}
        self._prices: dict[str, float] = {}
        self._orders = 0

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    async def connect(self) -> bool:
        return True

    async def last_price(self, symbol: str) -> float | None:
        """Latest price for ``symbol``, refreshed from ``quotes`` if configured.

        The fetched price is cached so that the order placed in response to a
        signal fills at the same price the signal was computed from. A quote
        that arrives *between* the two would be a fill at a price the strategy
        never saw - small, but it is look-ahead, and this is the layer that is
        supposed to be honest about that.

        A failed fetch falls back to the last known price rather than dropping
        the symbol, and the staleness surfaces in the trial report as a cycle
        error rather than being silently smoothed over.
        """
        if self.quotes is None:
            return self._prices.get(symbol)

        try:
            fresh = await self.quotes.last_price(symbol)
        except Exception as exc:                       # noqa: BLE001
            log.warning("[paper] quote for %s failed: %s", symbol, exc)
            fresh = None

        if fresh is not None and fresh > 0:
            self._prices[symbol] = fresh
        return self._prices.get(symbol)

    async def close(self) -> None:
        if self.quotes is not None and hasattr(self.quotes, "close"):
            await self.quotes.close()

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
