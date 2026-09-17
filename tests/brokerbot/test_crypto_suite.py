"""The rebuilt DaviddTech strategy suite."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from brokerbot.backtest.engine import BacktestEngine
from brokerbot.costs import PRESETS
from brokerbot.models import Bar
from brokerbot.strategy.crypto_suite import SUITE, RegimeGated

START = datetime(2020, 1, 1)


def path(fn, n=600, symbol="X"):
    """Synthetic bars whose intrabar range reflects the move that made them.

    A fixed +-2% wick on every bar is not a harmless simplification: Donchian
    compares the close against prior bar *highs*, so a drift of 0.4% a day can
    never clear a 2% wick and the rule silently never fires. The fixture has
    to be the shape of the data, or the test measures the fixture.
    """
    out, price = [], 100.0
    for i in range(n):
        previous = price
        price = fn(i, price)
        move = abs(price - previous)
        wick = move * 0.4 + price * 0.001
        out.append(Bar(symbol, START + timedelta(days=i), previous,
                       max(previous, price) + wick, min(previous, price) - wick,
                       price, 1_000.0 + i))
    return out


def test_every_strategy_is_long_only_and_regime_gated():
    """His whole dossier is long-only behind a bull filter. That gate is not a
    detail - in 2022 it is the entire difference between the strategies and
    buy-and-hold, because it is what put them in cash."""
    for name, cls in SUITE.items():
        assert issubclass(cls, RegimeGated), name


def test_nothing_goes_long_in_a_downtrend():
    """Below the regime filter, every rule must sit out. If any of them buys
    here, the 2022 result was luck rather than the gate doing its job."""
    falling = path(lambda i, p: p * 0.995)
    for name, cls in SUITE.items():
        signal = cls().on_bar("X", falling)
        assert signal is None or signal.target_weight == 0.0, name


def test_weights_never_exceed_fully_invested():
    rising = path(lambda i, p: p * 1.004 * (1.01 if i % 3 else 0.99))
    for name, cls in SUITE.items():
        s = cls().on_bar("X", rising)
        if s is not None:
            assert 0.0 <= s.target_weight <= 1.0, name


def test_donchian_compares_against_prior_bars_only():
    """Including the current bar in the lookback high makes the breakout
    condition trivially true on any new high, so it would fire every bar."""
    from brokerbot.strategy.crypto_suite import DonchianBreakout
    # Trend with pullbacks, so only some bars are genuine breakouts.
    import random
    rng = random.Random(5)
    bars = path(lambda i, p: p * (1.0025 + rng.gauss(0, 0.012)))
    strat = DonchianBreakout(entry=20, exit=10, regime=200)
    signals = [strat.on_bar("X", bars[:i]) for i in range(250, len(bars))]
    longs = sum(1 for s in signals if s and s.target_weight > 0)
    assert longs > 0, "never fired - the breakout condition is unreachable"
    assert longs < len(signals), "fired on every bar - lookback includes today"


def test_the_deadband_actually_suppresses_trading():
    """Without it, price hovering on the line flips the position every bar and
    costs eat the strategy alive."""
    from brokerbot.strategy.crypto_suite import GaussianDeadband

    import random
    rng = random.Random(11)
    chop = path(lambda i, p: p * (1.0008 + rng.gauss(0, 0.035)))

    def flips(deadband):
        strat = GaussianDeadband(deadband=deadband)
        signals = [strat.on_bar("X", chop[:i]) for i in range(210, len(chop))]
        return sum(1 for a, b in zip(signals, signals[1:])
                   if a and b and a.target_weight != b.target_weight)

    assert flips(0.0) > 0, "fixture is not choppy enough to flip at all"
    assert flips(0.08) < flips(0.0)


def test_strategies_run_end_to_end_through_the_costed_engine():
    bars = path(lambda i, p: p * (1.003 if (i // 90) % 2 else 0.997))
    for name, cls in SUITE.items():
        report = BacktestEngine(PRESETS["crypto_spot"], starting_cash=10_000) \
            .compare_to_benchmark(cls(), bars, symbol="X")
        assert report.benchmark.total_return != 0.0, name
