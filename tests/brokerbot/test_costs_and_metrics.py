"""Cost model and performance metrics."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from brokerbot.backtest.metrics import (
    ComparisonReport, Metrics, compute, max_drawdown,
)
from brokerbot.costs import PRESETS, CostModel
from brokerbot.models import ClosedTrade, EquityPoint

START = datetime(2020, 1, 1)


def equity(values: list[float]) -> list[EquityPoint]:
    return [
        EquityPoint(START + timedelta(days=i), cash=v, positions_value=0.0)
        for i, v in enumerate(values)
    ]


def trade(pnl: float, *, entry=100.0, qty=10.0, costs=0.0) -> ClosedTrade:
    return ClosedTrade(
        symbol="X", entry_ts=START, exit_ts=START + timedelta(days=5),
        quantity=qty, entry_price=entry, exit_price=entry + pnl / qty, costs=costs,
    )


# --- costs ---------------------------------------------------------------
def test_commission_minimum_dominates_small_orders():
    """Why trading 500 SEK positions is close to hopeless."""
    m = CostModel(commission_pct=0.0015, commission_min=1.0)
    assert m.commission(100) == 1.0            # minimum binds
    assert m.commission(100_000) == 150.0      # percentage binds


def test_spread_is_charged_as_half_per_side():
    m = CostModel(commission_pct=0, commission_min=0, spread_pct=0.01,
                  slippage_pct=0, fx_pct=0)
    assert m.spread_cost(10_000) == pytest.approx(50.0)
    assert m.round_trip_pct(10_000) == pytest.approx(0.01)


def test_fx_only_applies_when_converting():
    m = PRESETS["us_equities_from_sek"]
    assert m.fx(10_000, needs_conversion=True) > 0
    assert m.fx(10_000, needs_conversion=False) == 0


def test_etoro_costs_far_more_per_round_trip_than_ibkr():
    etoro = PRESETS["etoro"].round_trip_pct(10_000, needs_conversion=True)
    ibkr = PRESETS["ibkr_us"].round_trip_pct(10_000, needs_conversion=True)
    assert etoro > ibkr * 4


# --- metrics -------------------------------------------------------------
def test_max_drawdown():
    assert max_drawdown(equity([100, 120, 60, 80])) == pytest.approx(0.5)
    assert max_drawdown(equity([100, 110, 120])) == pytest.approx(0.0)


def test_total_return_and_cagr():
    m = compute(equity([100_000] + [200_000] * 365), [])
    assert m.total_return == pytest.approx(1.0)
    assert m.cagr == pytest.approx(1.0, abs=0.05)


def test_profit_factor_and_expectancy():
    m = compute(equity([100, 110]), [trade(100), trade(100), trade(-50)])
    assert m.profit_factor == pytest.approx(4.0)
    assert m.win_rate == pytest.approx(2 / 3)
    assert m.expectancy == pytest.approx(50.0)


def test_profit_factor_with_no_losses_is_infinite():
    assert compute(equity([100, 110]), [trade(50)]).profit_factor == math.inf


def test_costs_are_tracked_as_drag():
    m = compute(equity([100_000, 105_000]), [trade(100, costs=500), trade(100, costs=500)])
    assert m.total_costs == 1000
    assert m.cost_drag == pytest.approx(0.01)


def test_best_trade_share_flags_concentration():
    m = compute(equity([100, 110]), [trade(1000), trade(10), trade(10)])
    assert m.best_trade_share > 0.9


def test_empty_series_is_safe():
    m = compute([], [])
    assert m.total_return == 0.0 and m.trades == 0


# --- benchmark comparison ------------------------------------------------
def _metrics(total_return: float, sharpe: float) -> Metrics:
    m = Metrics()
    m.total_return = total_return
    m.sharpe = sharpe
    return m


def test_beating_benchmark_requires_return_and_risk_adjusted_return():
    """Higher return bought with more risk is not skill."""
    better = ComparisonReport(_metrics(0.30, 1.2), _metrics(0.10, 0.8))
    riskier = ComparisonReport(_metrics(0.30, 0.4), _metrics(0.10, 0.8))
    worse = ComparisonReport(_metrics(0.05, 0.3), _metrics(0.10, 0.8))

    assert better.beats_benchmark
    assert not riskier.beats_benchmark
    assert "more risk" in riskier.verdict()
    assert not worse.beats_benchmark
    assert "does NOT beat" in worse.verdict()
