"""Pine Script export.

The point of exporting is cross-validation: two independent backtesters
disagreeing on the same rule and the same bars means one of them is wrong.
These tests pin the settings that make the comparison valid at all - if the
generated script fills on the signal bar's close, or carries zero commission,
it is not measuring the same thing as our engine and any agreement is
coincidence.
"""

from __future__ import annotations

import pytest

from brokerbot.costs import PRESETS
from brokerbot.pine import TESTER_TRAPS, export, traps_text
from brokerbot.strategy.library import (
    BuyAndHold, MeanReversion, Momentum, PriceVsSma, SmaCrossover,
)
from brokerbot.strategy.news_drift import NewsDriftStrategy

ALL = [PriceVsSma(50), SmaCrossover(20, 60), Momentum(126), MeanReversion(20),
       BuyAndHold()]


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_every_chart_strategy_exports(strategy):
    script = export(strategy, PRESETS["nordic_equities"])
    assert script.startswith("//@version=5")
    assert "strategy(" in script


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_fills_on_next_bar_open(strategy):
    """The tester's default fills on the close of the signal bar - a price you
    could not have traded at. Our engine fills at the next open, so the Pine
    version must too or the two are measuring different strategies."""
    assert "process_orders_on_close=false" in export(
        strategy, PRESETS["nordic_equities"]
    )


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_costs_are_never_left_at_zero(strategy):
    """Commission and slippage default to zero in the Strategy Tester. That
    single omission is how a losing strategy shows a rising equity curve."""
    script = export(strategy, PRESETS["nordic_equities"])
    assert "commission_value=0.15" in script
    assert "slippage=0" not in script


def test_zero_cost_preset_is_honest_about_itself():
    script = export(PriceVsSma(50), PRESETS["zero"])
    assert "commission_value=0.0" in script


def test_costs_come_from_the_chosen_preset():
    nordic = export(PriceVsSma(50), PRESETS["nordic_equities"])
    etoro = export(PriceVsSma(50), PRESETS["etoro"])
    assert "commission_value=0.15" in nordic
    assert "commission_value=0.0" in etoro      # eToro's cost is the spread
    assert nordic != etoro


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_results_are_reproducible(strategy):
    """calc_on_every_tick makes results depend on when you loaded the chart."""
    assert "calc_on_every_tick=false" in export(strategy, PRESETS["ibkr_us"])


def test_sizing_matches_our_engine():
    script = export(PriceVsSma(50), PRESETS["ibkr_us"])
    assert "default_qty_type=strategy.percent_of_equity" in script


def test_parameters_are_carried_through():
    assert 'input.int(200, "SMA length")' in export(
        PriceVsSma(200), PRESETS["zero"]
    )
    cross = export(SmaCrossover(10, 30), PRESETS["zero"])
    assert 'input.int(10, "Fast SMA")' in cross
    assert 'input.int(30, "Slow SMA")' in cross


def test_buy_and_hold_never_exits():
    script = export(BuyAndHold(), PRESETS["zero"])
    assert "strategy.entry" in script
    assert "strategy.close" not in script


def test_externally_driven_strategy_is_refused():
    """Pine Script only sees the chart. A news-driven strategy cannot be
    expressed in it, and silently exporting a chart-only approximation would
    make the cross-check meaningless."""
    with pytest.raises(ValueError, match="external data"):
        export(NewsDriftStrategy(), PRESETS["zero"])


def test_traps_documented():
    text = traps_text()
    assert len(TESTER_TRAPS) >= 6
    for keyword in ("Heikin Ashi", "lookahead", "Repainting", "ZERO",
                    "selection bias"):
        assert keyword in text
