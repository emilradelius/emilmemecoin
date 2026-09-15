"""Event-driven backtest engine.

Two design decisions do almost all the work of keeping results honest:

**1. Signals are acted on at the NEXT bar's open, never the current close.**
A strategy that decides on Monday's close and fills at Monday's close is
trading on information it did not have at the time. That single mistake is
responsible for a large share of backtests that look extraordinary and then
fail live. Here the engine computes a signal from bar *i* and fills it at bar
*i+1*'s open, which is the earliest price genuinely reachable.

**2. Strategies receive history, never the future.** ``on_bar`` is handed a
list that ends at the current bar. Peeking is not merely discouraged, it is
structurally unavailable.

Costs are charged on every fill through :class:`~brokerbot.costs.CostModel`,
and the same engine runs the buy-and-hold benchmark through the identical
cost path, so the comparison is like-for-like.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..costs import CostModel
from ..models import (
    Bar, ClosedTrade, EquityPoint, Fill, Order, OrderType, PositionState, Side,
)
from ..strategy.base import Strategy
from .metrics import ComparisonReport, Metrics, compute

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    equity: list[EquityPoint] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    rejected_orders: int = 0
    skipped_small_orders: int = 0


class Portfolio:
    def __init__(self, starting_cash: float, costs: CostModel,
                 *, needs_fx: bool = False) -> None:
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self.costs = costs
        self.needs_fx = needs_fx
        self.positions: dict[str, PositionState] = {}
        self.open_entries: dict[str, tuple[datetime, float, float]] = {}
        self.trades: list[ClosedTrade] = []
        self.fills: list[Fill] = []
        self.accrued_costs: dict[str, float] = {}

    def value(self, prices: dict[str, float]) -> float:
        held = sum(
            p.market_value(prices.get(sym, p.avg_price))
            for sym, p in self.positions.items()
        )
        return self.cash + held

    def position(self, symbol: str) -> PositionState:
        return self.positions.setdefault(symbol, PositionState(symbol))

    def max_affordable_qty(self, price: float) -> float:
        """Largest quantity buyable with current cash, costs included.

        Needed because a target weight of 1.0 asks to spend the entire
        portfolio value on shares, leaving nothing for commission. Without
        this, every fully-invested order is rejected and the backtest silently
        does nothing at all - which looks like a flat strategy rather than a
        bug.
        """
        if price <= 0:
            return 0.0
        rate = (
            self.costs.commission_pct
            + self.costs.spread_pct / 2.0
            + self.costs.slippage_pct
            + (self.costs.fx_pct if self.needs_fx else 0.0)
        )
        usable = max(0.0, self.cash - self.costs.commission_min)
        return usable / (price * (1.0 + rate))

    def execute(self, order: Order, price: float, ts: datetime) -> Fill | None:
        value = order.quantity * price
        commission = self.costs.commission(value)
        spread = self.costs.spread_cost(value)
        slip = self.costs.slippage(value)
        fx = self.costs.fx(value, needs_conversion=self.needs_fx)

        # The fill price itself moves against you; spread and slippage are
        # booked as explicit costs rather than hidden in the price so the
        # report can show what they actually took.
        fill = Fill(
            order_id=order.id, symbol=order.symbol, side=order.side,
            quantity=order.quantity, price=price, ts=ts,
            commission=commission, slippage_cost=spread + slip, fx_cost=fx,
        )

        if order.side is Side.BUY and self.cash + fill.cash_delta < 0:
            log.debug("rejecting buy of %s: insufficient cash", order.symbol)
            return None

        pos = self.position(order.symbol)
        if order.side is Side.BUY:
            new_qty = pos.quantity + order.quantity
            if pos.quantity <= 0:
                self.open_entries[order.symbol] = (ts, price, order.quantity)
                self.accrued_costs[order.symbol] = fill.total_costs
            else:
                pos.avg_price = (
                    pos.avg_price * pos.quantity + price * order.quantity
                ) / new_qty if new_qty else price
                self.accrued_costs[order.symbol] = (
                    self.accrued_costs.get(order.symbol, 0.0) + fill.total_costs
                )
            if pos.quantity <= 0:
                pos.avg_price = price
            pos.quantity = new_qty
        else:
            sold = min(order.quantity, pos.quantity)
            if sold <= 0:
                return None
            entry = self.open_entries.get(order.symbol)
            costs = self.accrued_costs.get(order.symbol, 0.0) + fill.total_costs
            if entry:
                entry_ts, entry_price, _ = entry
                self.trades.append(ClosedTrade(
                    symbol=order.symbol, entry_ts=entry_ts, exit_ts=ts,
                    quantity=sold, entry_price=entry_price, exit_price=price,
                    costs=costs,
                ))
            pos.quantity -= sold
            if not pos.is_open:
                pos.quantity = 0.0
                pos.avg_price = 0.0
                self.open_entries.pop(order.symbol, None)
                self.accrued_costs.pop(order.symbol, None)

        self.cash += fill.cash_delta
        self.fills.append(fill)
        return fill


class BacktestEngine:
    def __init__(
        self,
        costs: CostModel,
        *,
        starting_cash: float = 100_000.0,
        needs_fx: bool = False,
        min_order_value: float = 500.0,
        risk_free_rate: float = 0.02,
    ) -> None:
        self.costs = costs
        self.starting_cash = starting_cash
        self.needs_fx = needs_fx
        # Orders below this are not worth placing: the commission minimum
        # eats them. Skipping them mirrors what a sane human would do and
        # stops the backtest from racking up trades nobody would make.
        self.min_order_value = min_order_value
        self.risk_free_rate = risk_free_rate

    def run(self, strategy: Strategy, bars: list[Bar]) -> BacktestResult:
        if len(bars) < 2:
            return BacktestResult()

        by_symbol: dict[str, list[Bar]] = {}
        for bar in bars:
            by_symbol.setdefault(bar.symbol, []).append(bar)
        for series in by_symbol.values():
            series.sort(key=lambda b: b.ts)

        timeline = sorted({b.ts for b in bars})
        portfolio = Portfolio(self.starting_cash, self.costs, needs_fx=self.needs_fx)
        result = BacktestResult()

        history: dict[str, list[Bar]] = {sym: [] for sym in by_symbol}
        index: dict[str, int] = {sym: 0 for sym in by_symbol}
        pending: list[tuple[str, float, str]] = []

        for step, ts in enumerate(timeline):
            prices: dict[str, float] = {}
            opens: dict[str, float] = {}

            for sym, series in by_symbol.items():
                i = index[sym]
                if i < len(series) and series[i].ts == ts:
                    bar = series[i]
                    history[sym].append(bar)
                    index[sym] = i + 1
                    prices[sym] = bar.close
                    opens[sym] = bar.open
                elif history[sym]:
                    prices[sym] = history[sym][-1].close

            # --- fill yesterday's decisions at today's open -----------------
            # This is the anti-look-ahead rule: a signal computed from the
            # previous bar's close can only be acted on at the next open.
            for sym, target_weight, reason in pending:
                fill_price = opens.get(sym)
                if fill_price is None:
                    continue
                self._rebalance(portfolio, sym, target_weight, fill_price, ts,
                                result, reason)
            pending = []

            # --- compute new signals from data available NOW ---------------
            is_last = step == len(timeline) - 1
            if not is_last:
                for sym in by_symbol:
                    series = history[sym]
                    if len(series) < max(1, strategy.warmup):
                        continue
                    signal = strategy.on_bar(sym, series)
                    if signal is not None:
                        pending.append((sym, signal.target_weight, signal.reason))

            result.equity.append(EquityPoint(
                ts=ts, cash=portfolio.cash,
                positions_value=portfolio.value(prices) - portfolio.cash,
            ))

        result.trades = portfolio.trades
        result.fills = portfolio.fills
        result.metrics = compute(result.equity, result.trades,
                                 fills=result.fills,
                                 risk_free_rate=self.risk_free_rate)
        return result

    def _rebalance(self, portfolio: Portfolio, symbol: str, target_weight: float,
                   price: float, ts: datetime, result: BacktestResult,
                   reason: str) -> None:
        if price <= 0:
            return
        equity = portfolio.value({symbol: price})
        pos = portfolio.position(symbol)
        target_qty = (equity * target_weight) / price
        delta = target_qty - pos.quantity
        if delta > 0:
            # Cap buys at what cash can actually cover once costs are paid.
            delta = min(delta, portfolio.max_affordable_qty(price))
        if abs(delta * price) < self.min_order_value:
            if abs(delta) > 1e-9:
                result.skipped_small_orders += 1
            return

        order = Order(
            symbol=symbol,
            side=Side.BUY if delta > 0 else Side.SELL,
            quantity=abs(delta),
            order_type=OrderType.MARKET,
            ts=ts,
            reason=reason,
        )
        if portfolio.execute(order, price, ts) is None:
            result.rejected_orders += 1

    def compare_to_benchmark(
        self, strategy: Strategy, bars: list[Bar], *, symbol: str = ""
    ) -> ComparisonReport:
        """Run the strategy and buy-and-hold through the identical cost path.

        Running the benchmark through the same engine matters: comparing a
        cost-laden strategy against a frictionless hold would understate the
        strategy, and comparing against a hold that pays per-trade costs it
        never incurs would flatter it.
        """
        from ..strategy.library import BuyAndHold

        strat = self.run(strategy, bars)
        bench = self.run(BuyAndHold(), bars)

        notes: list[str] = []
        if strat.skipped_small_orders:
            notes.append(
                f"{strat.skipped_small_orders} orders skipped as too small to "
                f"be worth the commission minimum"
            )
        if strat.rejected_orders:
            notes.append(f"{strat.rejected_orders} orders rejected for insufficient cash")
        if strat.metrics.cost_drag > 0.05:
            notes.append(
                f"costs consumed {strat.metrics.cost_drag:.1%} of starting capital"
            )
        if strat.metrics.best_trade_share > 0.5:
            notes.append(
                f"{strat.metrics.best_trade_share:.0%} of gross profit came from a "
                f"single trade - the result rests on one outcome"
            )

        return ComparisonReport(
            strategy=strat.metrics, benchmark=bench.metrics,
            symbol=symbol, label=strategy.describe(), notes=notes,
        )
