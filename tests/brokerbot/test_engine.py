"""Backtest engine correctness.

These tests exist to protect the properties that make a backtest trustworthy.
A backtester that is subtly wrong is worse than none at all: it produces
confident numbers that justify risking money.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from brokerbot.backtest.engine import BacktestEngine
from brokerbot.costs import PRESETS, CostModel
from brokerbot.data.synthetic import random_walk
from brokerbot.models import Bar, Side
from brokerbot.strategy.base import Signal, Strategy
from brokerbot.strategy.library import BuyAndHold, MeanReversion, SmaCrossover

START = datetime(2020, 1, 1)


class AlwaysLong(Strategy):
    name = "always_long"

    def on_bar(self, symbol, history):
        return Signal(symbol, 1.0, "always")


class Flat(Strategy):
    name = "flat"

    def on_bar(self, symbol, history):
        return Signal(symbol, 0.0, "never")


def gapped_series(n: int, gap: float) -> list[Bar]:
    """Each bar opens `gap` above the previous close, and is otherwise flat.

    Makes look-ahead unmistakable: filling at the decision close instead of
    the next open captures the whole gap for free.
    """
    bars, price = [], 100.0
    for i in range(n):
        o = price * (1 + gap)
        c = o
        bars.append(Bar("TEST", START + timedelta(days=i), o, o * 1.001, o * 0.999, c))
        price = c
    return bars


@pytest.fixture
def engine() -> BacktestEngine:
    return BacktestEngine(PRESETS["zero"], starting_cash=100_000, min_order_value=1)


def test_fills_happen_at_next_bar_open(engine):
    """The single most important property. A signal computed from bar i's
    close must fill at bar i+1's open - the earliest genuinely reachable
    price."""
    bars = gapped_series(20, gap=0.05)
    result = engine.run(AlwaysLong(), bars)
    assert result.fills
    assert result.fills[0].price == pytest.approx(bars[1].open)
    assert result.fills[0].price != pytest.approx(bars[0].close)


def test_strategy_never_receives_future_bars(engine):
    seen: list[int] = []

    class Recorder(Strategy):
        name = "recorder"

        def on_bar(self, symbol, history):
            seen.append(len(history))
            # Every bar handed over must already have happened.
            assert all(b.ts <= history[-1].ts for b in history)
            return None

    bars = random_walk(bars=50, seed=1)
    engine.run(Recorder(), bars)
    assert seen and max(seen) <= len(bars)


def test_full_investment_is_affordable_with_costs():
    """A target weight of 1.0 asks to spend the whole portfolio on shares,
    leaving nothing for commission. Without capping the order to what cash
    covers, every trade is rejected and the backtest silently does nothing -
    which looks like a flat strategy rather than a bug."""
    engine = BacktestEngine(PRESETS["nordic_equities"], starting_cash=100_000)
    result = engine.run(AlwaysLong(), random_walk(bars=100, seed=3))
    assert result.fills, "no fills - the affordability cap regressed"
    assert result.rejected_orders == 0


def test_costs_reduce_returns_monotonically():
    bars = random_walk(bars=800, seed=11, drift=0.0004)
    returns = {}
    for name in ("zero", "ibkr_us", "nordic_equities", "etoro"):
        cm = PRESETS[name]
        eng = BacktestEngine(cm, starting_cash=100_000, needs_fx=cm.fx_pct > 0)
        returns[name] = eng.run(MeanReversion(), bars).metrics.total_return
    assert returns["zero"] > returns["ibkr_us"] > returns["nordic_equities"] > returns["etoro"]


def test_flat_strategy_never_trades(engine):
    result = engine.run(Flat(), random_walk(bars=100, seed=2))
    assert not result.fills
    assert result.metrics.total_return == pytest.approx(0.0)


def test_buy_and_hold_tracks_the_instrument():
    engine = BacktestEngine(PRESETS["zero"], starting_cash=100_000, min_order_value=1)
    bars = random_walk(bars=300, seed=5, drift=0.001)
    result = engine.run(BuyAndHold(), bars)
    instrument_return = bars[-1].close / bars[1].open - 1.0
    assert result.metrics.total_return == pytest.approx(instrument_return, abs=0.02)
    assert result.metrics.trades == 0  # never sells, so nothing closes


def test_benchmark_runs_through_the_same_cost_path():
    """Comparing a cost-laden strategy against a frictionless hold would
    understate the strategy; the comparison must be like-for-like."""
    engine = BacktestEngine(PRESETS["nordic_equities"], starting_cash=100_000)
    report = engine.compare_to_benchmark(SmaCrossover(10, 30),
                                         random_walk(bars=600, seed=9))
    assert report.benchmark.total_costs > 0


def test_small_orders_are_skipped_not_executed():
    engine = BacktestEngine(PRESETS["nordic_equities"], starting_cash=100_000,
                            min_order_value=1_000_000)
    result = engine.run(AlwaysLong(), random_walk(bars=50, seed=4))
    assert not result.fills
    assert result.skipped_small_orders > 0


def test_cash_and_positions_reconcile(engine):
    bars = random_walk(bars=200, seed=6)
    result = engine.run(SmaCrossover(5, 20), bars)
    for point in result.equity:
        assert point.total == pytest.approx(point.cash + point.positions_value)
        assert point.total > 0


def test_insufficient_history_returns_empty(engine):
    assert engine.run(AlwaysLong(), []).equity == []
    assert engine.run(AlwaysLong(), random_walk(bars=1)).equity == []
