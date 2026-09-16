"""Time-series momentum: the specification, and the ways it could cheat."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from brokerbot.models import Bar
from brokerbot.strategy.tsmom import (
    TimeSeriesMomentum, TsmomResult, ewma_volatility, month_ends,
)

START = datetime(2010, 1, 1)


def ramp(symbol, days, fn, noise=0.008, seed=7):
    """A price path with a trend and real daily noise.

    The noise is not decoration. Position size is target_vol / realised_vol,
    so a perfectly smooth path has zero volatility, infinite size and is
    rejected outright - a fixture without it produces no trades at all and
    every assertion below passes vacuously.
    """
    import random
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(days):
        price = fn(i, price) * (1 + rng.gauss(0, noise))
        ts = START + timedelta(days=i)
        out.append(Bar(symbol, ts, price, price * 1.001, price * 0.999, price, 1e6))
    return out


def test_the_fixture_produces_tradeable_volatility():
    """Guards the fixture. Without noise the strategy takes no positions and
    every test in this file passes by doing nothing."""
    bars = ramp("X", 500, lambda i, p: p * 1.0008)
    rets = [bars[i].close / bars[i - 1].close - 1 for i in range(1, len(bars))]
    assert ewma_volatility(rets) > 0.02


# --- volatility estimate --------------------------------------------------
def test_volatility_is_annualised_and_roughly_right():
    """A series with 1% daily moves should read about 16% annualised
    (0.01 * sqrt(261)), not 1% and not 100%."""
    rets = [0.01 if i % 2 else -0.01 for i in range(400)]
    vol = ewma_volatility(rets)
    assert vol == pytest.approx(0.01 * math.sqrt(261), rel=0.15)


def test_recent_volatility_dominates_the_estimate():
    """Sixty-day centre of mass means a calm year does not hide this month's
    panic - that is the whole point of sizing on ex-ante volatility."""
    calm = [0.001] * 300
    assert ewma_volatility(calm + [0.05] * 30) > 4 * ewma_volatility(calm)


def test_a_flat_series_has_no_volatility_and_is_not_traded():
    assert ewma_volatility([0.0] * 100) == 0.0


# --- calendar -------------------------------------------------------------
def test_month_ends_picks_the_last_bar_of_each_month():
    bars = ramp("X", 70, lambda i, p: p)
    ends = month_ends(bars)
    months = {(bars[i].ts.year, bars[i].ts.month) for i in ends}
    assert len(ends) == len(months)
    for i in ends[:-1]:
        assert bars[i + 1].ts.month != bars[i].ts.month


# --- the signal -----------------------------------------------------------
def test_a_rising_market_is_held_long_and_a_falling_one_is_shorted():
    up = ramp("UP", 900, lambda i, p: p * 1.0008)
    down = ramp("DN", 900, lambda i, p: p * 0.9992)

    long_only = TimeSeriesMomentum(long_only=True, cost_bps=0)
    both = TimeSeriesMomentum(cost_bps=0)

    # Shorting a falling market must make money; refusing to must make none.
    assert both.run({"DN": down}).total_return > 0
    assert long_only.run({"DN": down}).n_positions == []
    assert both.run({"UP": up}).total_return > 0


def test_position_size_falls_as_volatility_rises():
    """Two markets trending equally, one twice as jumpy. Equal weight would
    let the jumpy one dominate the portfolio's risk."""
    strat = TimeSeriesMomentum(cost_bps=0)
    calm = ramp("C", 900, lambda i, p: p * 1.0008, noise=0.004)
    wild = ramp("W", 900, lambda i, p: p * 1.0008, noise=0.020)
    vc = ewma_volatility([calm[i].close / calm[i-1].close - 1 for i in range(1, 300)])
    vw = ewma_volatility([wild[i].close / wild[i-1].close - 1 for i in range(1, 300)])
    assert vw > vc
    assert min(strat.target_vol / vw, strat.max_leverage) < \
           min(strat.target_vol / vc, strat.max_leverage)


# --- look-ahead -----------------------------------------------------------
def test_the_result_does_not_change_when_the_future_is_removed():
    """Truncating the data must leave the earlier months exactly as they were.
    If a later bar can alter an earlier month's return, something is reading
    forward."""
    bars = ramp("X", 1400, lambda i, p: p * (1.0006 if (i // 40) % 3 else 0.9990))

    # Cut on a month boundary. Slicing mid-month makes that month's "end" the
    # truncation point, so its return covers four days instead of thirty-one
    # and differs for an honest reason - which would mask a real peek rather
    # than reveal one.
    cut = max(i for i in range(len(bars) - 1)
              if bars[i].ts.month != bars[i + 1].ts.month and i < 1100) + 1

    full = TimeSeriesMomentum(cost_bps=0).run({"X": bars})
    short = TimeSeriesMomentum(cost_bps=0).run({"X": bars[:cut]})
    assert len(short.returns) < len(full.returns)
    assert short.months == full.months[:len(short.months)]
    for i in range(len(short.returns)):
        assert short.returns[i] == pytest.approx(full.returns[i], abs=1e-12)


def test_ex_post_volatility_scaling_is_off_by_default():
    """It rescales by a number measured over the whole sample. Harmless for
    Sharpe, dishonest for a quoted return, so it must never be the default."""
    assert TimeSeriesMomentum().portfolio_vol is None


# --- costs ----------------------------------------------------------------
def test_costs_are_charged_on_turnover_not_on_holdings():
    """The strategy re-sizes every month and flips whole positions. A cost
    model blind to turnover measures a portfolio nobody can hold."""
    bars = ramp("X", 1400, lambda i, p: p * (1.0006 if (i // 30) % 2 else 0.9990))
    free = TimeSeriesMomentum(cost_bps=0).run({"X": bars})
    paid = TimeSeriesMomentum(cost_bps=50).run({"X": bars})
    assert paid.total_return < free.total_return
    assert sum(paid.turnover) > 0
