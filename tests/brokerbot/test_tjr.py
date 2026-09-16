"""The TJR sweep-and-shift model and the bracket engine.

The model is worth testing for one reason above all others: it is a
multi-step, stateful, time-of-day strategy, which is the shape of strategy
where look-ahead bias hides most comfortably. A pivot used one bar before it
was confirmed turns a coin flip into a machine that prints money.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from brokerbot.backtest.bracket import BracketEngine
from brokerbot.models import Bar
from brokerbot.strategy.tjr import NY, Setup, TjrModel, to_ny

# 13:00 UTC is 09:00 New York whatever the season.
DAY = datetime(2026, 3, 10)


def bar(ts, o, h, l, c, sym="NQ"):
    return Bar(sym, ts, o, h, l, c, 1000.0)


def series(start: datetime, prices, step_min=5):
    """OHLC bars from (o,h,l,c) tuples, five minutes apart."""
    return [bar(start + timedelta(minutes=step_min * i), *p)
            for i, p in enumerate(prices)]


# --- session handling -----------------------------------------------------
def test_session_boundaries_follow_new_york_across_a_dst_change():
    """London opens at 03:00 New York. Stockholm and New York switch to
    summer time on different dates, so a UTC-hour comparison moves the whole
    session by an hour for two weeks a year."""
    winter = datetime(2026, 1, 15, 8, 0)     # 08:00 UTC
    summer = datetime(2026, 7, 15, 7, 0)     # 07:00 UTC
    assert to_ny(winter).hour == 3
    assert to_ny(summer).hour == 3


# --- look-ahead -----------------------------------------------------------
def test_a_pivot_is_not_usable_before_it_is_confirmed():
    """A pivot centred on bar j is only visible once j+k has printed. Using
    it at j is look-ahead, and it is the difference between a strategy and a
    time machine."""
    model = TjrModel(pivot_strength=2)
    bars = series(DAY, [
        (100, 101, 99, 100), (100, 102, 99, 101), (101, 110, 100, 109),
        (109, 110, 108, 109), (109, 110, 108, 109),
    ])
    # Bar 2 is the highest, but with k=2 it cannot be confirmed until bar 4.
    assert model._last_confirmed_pivot(bars, now=3, high=True, after=0) is None
    assert model._last_confirmed_pivot(bars, now=6, high=True, after=0) == 110


def test_scanning_a_day_never_reads_a_bar_after_the_signal():
    """Truncating the data at the signal bar must not change the setup. If it
    does, something downstream of the decision fed back into it."""
    bars = _synthetic_long_day()
    model = TjrModel(pivot_strength=2)
    full = model.find_setups(bars)
    assert full, "fixture should produce a setup"
    setup = full[0]

    cut = [b for b in bars if b.ts <= setup.signal_ts]
    again = model.find_setups(cut)
    assert again, "setup vanished when later bars were removed"
    assert again[0].entry == pytest.approx(setup.entry)
    assert again[0].stop == pytest.approx(setup.stop)


# --- sweep definition -----------------------------------------------------
def test_a_clean_break_is_not_a_sweep():
    """Price closing beyond the level broke it. A sweep closes back inside -
    that is the whole distinction, and it is only knowable at the close."""
    model = TjrModel()
    bars = _asia(90, 110) + series(
        DAY.replace(hour=8), [(109, 120, 108, 119)] * 25)   # closes above
    assert model.find_setups(bars) == []


# --- bracket engine -------------------------------------------------------
def _setup(entry=100.0, stop=98.0, target=106.0, direction=1):
    ts = DAY.replace(hour=13)
    return Setup(day=to_ny(ts).date(), direction=direction, signal_ts=ts,
                 entry=entry, stop=stop, target=target,
                 asia_high=110, asia_low=90, sweep_ts=ts, mss_ts=ts)


def test_a_bar_holding_both_stop_and_target_is_scored_as_a_loss():
    """Five-minute bars do not say which came first. Assuming the good one
    every time is the easiest way to manufacture an edge that is not there."""
    s = _setup()
    ts = DAY.replace(hour=13)
    bars = [bar(ts, 101, 101, 101, 101),
            bar(ts + timedelta(minutes=5), 100, 107, 97, 104)]  # both levels
    res = BracketEngine(slippage_ticks=0, commission_per_side=0).run(bars, [s])
    assert len(res.trades) == 1
    assert res.trades[0].outcome == "stop"
    assert res.trades[0].pnl < 0


def test_an_unfilled_limit_is_counted_not_silently_dropped():
    """Setups that never filled are the difference between 'this model is
    selective' and 'this model almost never trades'."""
    s = _setup(entry=50.0)
    ts = DAY.replace(hour=13)
    bars = [bar(ts + timedelta(minutes=5 * i), 100, 101, 99, 100) for i in range(6)]
    res = BracketEngine().run(bars, [s])
    assert res.trades == []
    assert res.setups_unfilled == 1
    assert res.setups_found == 1


def test_position_size_puts_the_same_money_at_risk_whatever_the_stop():
    """1% risk means 1%. A wider stop must buy fewer contracts, or the widest
    setup quietly becomes the biggest bet."""
    eng = BracketEngine(starting_cash=100_000, risk_pct=0.01, point_value=20.0,
                        slippage_ticks=0, commission_per_side=0)
    ts = DAY.replace(hour=13)

    def risked(stop):
        s = _setup(entry=100.0, stop=stop, target=200.0)
        bars = [bar(ts, 101, 101, 101, 101),
                bar(ts + timedelta(minutes=5), 100, 100, stop - 1, stop - 1)]
        res = eng.run(bars, [s])
        return -res.trades[0].pnl

    assert risked(98.0) == pytest.approx(1000.0, rel=0.02)
    assert risked(95.0) == pytest.approx(1000.0, rel=0.02)


def test_nothing_is_carried_overnight():
    s = _setup(entry=100.0, stop=90.0, target=200.0)
    bars = [bar(DAY.replace(hour=13), 101, 101, 101, 101)]
    bars += [bar(DAY.replace(hour=13) + timedelta(minutes=5 * i), 100, 101, 99, 100)
             for i in range(1, 40)]
    bars.append(bar(DAY.replace(hour=21), 100, 101, 99, 100))   # 17:00 NY
    res = BracketEngine(slippage_ticks=0, commission_per_side=0).run(bars, [s])
    assert res.trades[0].outcome == "timeout"


# --- fixtures -------------------------------------------------------------
def _asia(low, high):
    """Asia session for trading day DAY: 20:00-24:00 New York the evening
    before, which in March (EDT, UTC-4) is 00:00-04:00 UTC *on* DAY."""
    return [bar(DAY.replace(hour=0) + timedelta(minutes=5 * i),
                (low + high) / 2, high, low, (low + high) / 2)
            for i in range(48)]


def test_the_asia_fixture_really_lands_in_the_asia_window():
    """Guards the fixture itself. Placed a day out, every scan returns nothing
    and every test below passes for the wrong reason."""
    hours = {to_ny(b.ts).hour for b in _asia(90, 110)}
    assert hours <= {20, 21, 22, 23}
    assert {to_ny(b.ts).date() for b in _asia(90, 110)} == {(DAY - timedelta(days=1)).date()}


def _synthetic_long_day():
    """Asia 90-110, a sweep below 90 that closes back inside, a shift up
    through a confirmed pivot high, and a gap for the retracement to fill."""
    bars = _asia(90, 110)
    t = DAY.replace(hour=8)                    # 04:00 NY, inside the window
    prices = [(95, 96, 94, 95)] * 4            # consolidation: pivot high 96
    prices += [(94, 95, 88, 93)]               # sweep: low 88 < 90, closes back
    prices += [(93, 94, 92, 93), (93, 95, 93, 94)]
    prices += [(94, 96, 94, 96)]               # displacement bar 1
    prices += [(96, 104, 96, 103)]             # breaks 96 (MSS) and gaps 95->96
    prices += [(103, 104, 95, 96)]             # retraces into the gap: fills
    prices += [(96, 111, 96, 110)]             # runs to the Asia high: target
    prices += [(110, 111, 109, 110)] * 14
    return bars + series(t, prices)


def test_a_stop_out_is_minus_one_R_not_minus_twenty():
    """R is a ratio of money to money. Dividing dollar P&L by a risk measured
    in index points scales every result by the contract's point value, so a
    textbook stop-out reports as -20R and the strategy looks catastrophic
    rather than merely losing."""
    s = _setup(entry=100.0, stop=98.0, target=106.0)
    ts = DAY.replace(hour=13)
    bars = [bar(ts, 101, 101, 101, 101),
            bar(ts + timedelta(minutes=5), 100, 100, 97, 97)]
    res = BracketEngine(point_value=20.0, slippage_ticks=0,
                        commission_per_side=0).run(bars, [s])
    assert res.trades[0].r_multiple == pytest.approx(-1.0, abs=0.01)


def test_hitting_target_pays_the_advertised_R():
    s = _setup(entry=100.0, stop=98.0, target=106.0)     # 3R
    ts = DAY.replace(hour=13)
    bars = [bar(ts, 101, 101, 101, 101),
            bar(ts + timedelta(minutes=5), 100, 100, 99, 99),
            bar(ts + timedelta(minutes=10), 100, 107, 100, 106)]
    res = BracketEngine(point_value=20.0, slippage_ticks=0,
                        commission_per_side=0).run(bars, [s])
    assert res.trades[0].r_multiple == pytest.approx(3.0, abs=0.01)
