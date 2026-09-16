"""Synthetic price generator: orthogonality, fat tails, and asset calibration.

The generator's job is to produce a believable zero-edge baseline. Two things
have to be true for that baseline to be worth anything, and both are tested
here:

1. ``drift``, ``volatility`` and ``trend_strength`` are independent knobs.
2. The paths resemble the asset class they claim to, rather than diverging.
"""

from __future__ import annotations

import math
import statistics

import pytest

from brokerbot.data.base import BarSource
from brokerbot.data.synthetic import (
    ASSET_PRESETS,
    _CALIBRATION_TARGETS,
    PathStats,
    calibrate,
    random_walk,
    summarise,
)


def log_returns(bars) -> list[float]:
    closes = [b.close for b in bars]
    return [math.log(b / a) for a, b in zip(closes, closes[1:])]


def pooled(seeds: int = 60, bars: int = 1500, **kwargs) -> list[float]:
    """Log returns pooled across seeds, to shrink the sampling error."""
    out: list[float] = []
    for seed in range(seeds):
        out.extend(log_returns(random_walk(bars=bars, seed=seed, **kwargs)))
    return out


# --- orthogonality: drift vs volatility -----------------------------------
# For a fixed seed the standardised innovations are identical whatever drift
# and volatility are set to - neither consumes randomness. That makes these
# two properties exactly checkable rather than merely statistical.
def test_drift_enters_additively_and_volatility_cannot_touch_it():
    """Regression test for the variance-driven geometric-return loss.

    Compounding simple returns loses about ``volatility**2 / 2`` of growth per
    bar, so raising volatility used to drag the path downwards even though
    ``drift`` never changed. Here the realised mean log return must exceed the
    zero-drift run by exactly ``log1p(drift)``, at every volatility.
    """
    drift = 0.0008
    for volatility in (0.005, 0.012, 0.035, 0.08):
        with_drift = statistics.fmean(
            log_returns(random_walk(bars=3000, seed=7, drift=drift,
                                    volatility=volatility))
        )
        without = statistics.fmean(
            log_returns(random_walk(bars=3000, seed=7, drift=0.0,
                                    volatility=volatility))
        )
        assert with_drift - without == pytest.approx(math.log1p(drift), abs=1e-12)


def test_median_terminal_price_is_volatility_independent():
    """The median path ends at ``start_price * (1 + drift) ** bars`` whatever
    the volatility. If volatility is eating growth, this collapses."""
    drift, bars = 0.0005, 2000
    expected = 100.0 * (1.0 + drift) ** bars

    for volatility in (0.008, 0.02, 0.05):
        finals = [
            random_walk(bars=bars, seed=s, drift=drift,
                        volatility=volatility)[-1].close
            for s in range(120)
        ]
        # Median rather than mean: the mean of a lognormal is dragged upwards
        # by its own tail, and more so the wider the tail.
        assert statistics.median(finals) == pytest.approx(expected, rel=0.35)


def test_volatility_is_drift_independent():
    for drift in (0.0, 0.0005, 0.003):
        spread = statistics.pstdev(
            log_returns(random_walk(bars=3000, seed=4, drift=drift,
                                    volatility=0.02))
        )
        assert spread == pytest.approx(0.02, rel=0.05)


def test_realised_volatility_matches_the_requested_figure():
    for volatility in (0.006, 0.012, 0.035):
        spread = statistics.pstdev(pooled(seeds=40, volatility=volatility))
        assert spread == pytest.approx(volatility, rel=0.04)


# --- orthogonality: trend_strength ----------------------------------------
def test_trend_strength_does_not_amplify_drift():
    """Regression test for drift amplification through the momentum
    accumulator.

    Feeding the accumulator the total per-bar return rather than the noise
    alone let ``trend_strength`` multiply the drift, so turning trend on
    quietly raised the expected return. The realised drift must not move.
    """
    drift = 0.0005
    baseline = statistics.fmean(pooled(seeds=80, drift=drift, trend_strength=0.0))

    for trend in (0.3, 0.6, 1.0, 2.0):
        realised = statistics.fmean(
            pooled(seeds=80, drift=drift, trend_strength=trend)
        )
        assert realised == pytest.approx(baseline, abs=1.5e-4)
        assert realised == pytest.approx(math.log1p(drift), abs=1.5e-4)


def test_trend_strength_does_not_inflate_volatility():
    """Momentum adds a second term to each return, so without renormalising
    it would raise the variance and masquerade as extra volatility."""
    for trend in (0.0, 0.3, 0.6, 1.0, 2.0):
        spread = statistics.pstdev(
            pooled(seeds=80, volatility=0.015, trend_strength=trend)
        )
        assert spread == pytest.approx(0.015, rel=0.04)


def test_trend_strength_actually_creates_momentum():
    """The orthogonality tests above would also pass if trend_strength did
    nothing at all. It has to show up as return autocorrelation."""

    def autocorrelation(series: list[float]) -> float:
        mean = statistics.fmean(series)
        centred = [x - mean for x in series]
        numerator = sum(a * b for a, b in zip(centred, centred[1:]))
        denominator = sum(x * x for x in centred)
        return numerator / denominator

    flat = autocorrelation(pooled(seeds=30, trend_strength=0.0))
    trending = autocorrelation(pooled(seeds=30, trend_strength=1.0))
    assert abs(flat) < 0.03
    assert trending > 0.10


# --- fat tails ------------------------------------------------------------
def test_student_t_has_fatter_tails_at_the_same_volatility():
    """The point of the option: excess kurtosis in the individual returns,
    without the realised volatility moving."""

    def excess_kurtosis(series: list[float]) -> float:
        mean = statistics.fmean(series)
        spread = statistics.pstdev(series)
        return statistics.fmean(((x - mean) / spread) ** 4 for x in series) - 3.0

    normal = pooled(seeds=40, volatility=0.02, distribution="normal")
    fat = pooled(seeds=40, volatility=0.02, distribution="student_t", df=4.0)

    assert excess_kurtosis(normal) < 0.5
    assert excess_kurtosis(fat) > 2.0
    assert statistics.pstdev(fat) == pytest.approx(0.02, rel=0.06)


def test_student_t_needs_finite_variance():
    with pytest.raises(ValueError, match="df > 2"):
        random_walk(bars=10, distribution="student_t", df=2.0)


def test_unknown_distribution_is_rejected():
    with pytest.raises(ValueError, match="unknown distribution"):
        random_walk(bars=10, distribution="cauchy")


# --- single-bar cap -------------------------------------------------------
def test_max_move_caps_every_bar():
    """The cap bounds the market shock, which at drift 0 is the whole bar."""
    cap = 0.10
    returns = log_returns(
        random_walk(bars=4000, seed=2, drift=0.0, volatility=0.05,
                    distribution="student_t", df=3.0, max_move=cap)
    )
    assert max(abs(r) for r in returns) <= math.log1p(cap) + 1e-12
    # Without a fat tail actually hitting the cap this proves nothing.
    assert max(abs(r) for r in returns) == pytest.approx(math.log1p(cap))


def test_cap_bounds_the_shock_not_the_drift():
    """The clamp is applied to the noise term only. Clamping the total return
    would make a binding cap asymmetric and quietly bleed away the drift."""
    cap, drift = 0.10, 0.002
    returns = log_returns(
        random_walk(bars=4000, seed=2, drift=drift, volatility=0.05,
                    distribution="student_t", df=3.0, max_move=cap)
    )
    bound = math.log1p(drift) + math.log1p(cap)
    assert max(abs(r) for r in returns) <= bound + 1e-12


def test_cap_binds_without_shifting_the_drift():
    """A symmetric clamp of a symmetric distribution trims the tails and
    leaves the mean alone, so the cap must not become a hidden drift."""
    uncapped = statistics.fmean(pooled(seeds=60, drift=0.0, volatility=0.04,
                                       distribution="student_t"))
    capped = statistics.fmean(pooled(seeds=60, drift=0.0, volatility=0.04,
                                     distribution="student_t", max_move=0.08))
    assert capped == pytest.approx(0.0, abs=2e-4)
    assert uncapped == pytest.approx(0.0, abs=2e-4)


def test_max_move_must_be_positive():
    with pytest.raises(ValueError, match="max_move"):
        random_walk(bars=10, max_move=0.0)


# --- volatility clustering ------------------------------------------------
def test_clustering_preserves_average_volatility():
    """Clustering redistributes volatility through time. It must not add
    any, or it would silently break the volatility knob."""
    for clustering in (0.0, 0.4, 0.8):
        spread = statistics.pstdev(
            pooled(seeds=60, volatility=0.02, vol_clustering=clustering)
        )
        assert spread == pytest.approx(0.02, rel=0.08)


def test_clustering_makes_volatility_arrive_in_bursts():
    """Without this the previous test would pass on a generator that ignores
    the parameter entirely."""

    def vol_of_vol(**kwargs) -> float:
        returns = log_returns(random_walk(bars=4000, seed=3, **kwargs))
        chunks = [returns[i:i + 50] for i in range(0, len(returns) - 50, 50)]
        return statistics.pstdev([statistics.pstdev(c) for c in chunks])

    assert vol_of_vol(volatility=0.02, vol_clustering=0.8) > \
        2.0 * vol_of_vol(volatility=0.02, vol_clustering=0.0)


# --- asset-class presets --------------------------------------------------
@pytest.mark.parametrize("preset", sorted(_CALIBRATION_TARGETS))
def test_presets_reproduce_their_asset_class(preset):
    """The headline property: a preset's paths have to have the *shape* of
    the asset class, measured as medians across many seeds."""
    stats = calibrate(preset, runs=40, bars=2500)
    for metric, (low, high) in _CALIBRATION_TARGETS[preset].items():
        assert low <= stats[metric] <= high, (
            f"{preset}.{metric} = {stats[metric]:.4f}, outside [{low}, {high}]"
        )


def test_crypto_drawdowns_land_in_a_realistic_band():
    """The bug this preset exists to fix.

    Gaussian returns compounded at crypto's ~3.5% daily volatility produced
    buy-and-hold drawdowns of 100% - the price went effectively to zero on
    many paths. Real Bitcoin's worst drawdown over 2015-2025 was about -83%.
    """
    drawdowns = sorted(
        summarise(random_walk(bars=2500, seed=s, preset="crypto")).max_drawdown
        for s in range(60)
    )

    assert 0.70 <= statistics.median(drawdowns) <= 0.85

    # No path may go effectively to zero. This is what used to fail.
    assert max(drawdowns) < 0.99

    # And the band has to hold for the bulk of seeds, not just at the median.
    in_band = sum(0.45 <= d <= 0.95 for d in drawdowns)
    assert in_band >= 0.8 * len(drawdowns)


def test_crypto_paths_are_not_mistaken_for_unadjusted_splits():
    """A cap of 30% keeps every bar well inside the validator's split
    heuristic, so a legitimate crypto series never trips the data warning."""
    for seed in range(10):
        assert BarSource.validate(
            random_walk("BTC", bars=1000, seed=seed, preset="crypto")
        ) == []


def test_crypto_noise_preset_has_no_edge_to_find():
    """The presets that model a real asset carry momentum on purpose, which
    makes them unsuitable as a zero-edge baseline. This one is the baseline,
    so its drift must be zero and its returns uncorrelated."""
    assert ASSET_PRESETS["crypto_noise"]["drift"] == 0.0
    assert ASSET_PRESETS["crypto_noise"]["trend_strength"] == 0.0

    returns = pooled(seeds=40, preset="crypto_noise")
    assert statistics.fmean(returns) == pytest.approx(0.0, abs=6e-4)
    assert statistics.pstdev(returns) == pytest.approx(0.035, rel=0.06)


def test_explicit_arguments_beat_the_preset():
    bars = random_walk(bars=2000, seed=1, preset="crypto", volatility=0.005)
    assert statistics.pstdev(log_returns(bars)) == pytest.approx(0.005, rel=0.15)


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError, match="unknown preset"):
        random_walk(bars=10, preset="tulips")


# --- basic contract -------------------------------------------------------
def test_default_parameters_are_gaussian_and_uncapped():
    """Default behaviour is unchanged by the new options: existing callers,
    and every noise baseline already published, keep their series."""
    returns = pooled(seeds=40)
    assert statistics.pstdev(returns) == pytest.approx(0.012, rel=0.04)
    assert statistics.fmean(returns) == pytest.approx(math.log1p(0.0003), abs=1e-4)


def test_the_same_seed_gives_the_same_series():
    a = random_walk("X", bars=200, seed=99)
    b = random_walk("X", bars=200, seed=99)
    assert [bar.close for bar in a] == [bar.close for bar in b]
    assert [bar.close for bar in random_walk("X", bars=200, seed=98)] != \
        [bar.close for bar in a]


def test_generated_bars_pass_their_own_validator():
    assert BarSource.validate(random_walk(bars=500, seed=1)) == []


def test_bars_are_internally_consistent():
    for bar in random_walk(bars=300, seed=12, volatility=0.03):
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= max(bar.open, bar.close)
        assert bar.low > 0
        assert bar.volume >= 0


def test_timestamps_are_weekdays_in_order():
    bars = random_walk(bars=200, seed=1)
    stamps = [b.ts for b in bars]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    assert all(b.ts.weekday() < 5 for b in bars)


def test_zero_volatility_gives_a_pure_drift_path():
    bars = random_walk(bars=100, seed=1, drift=0.001, volatility=0.0)
    assert bars[-1].close == pytest.approx(100.0 * 1.001 ** 100, rel=1e-9)


def test_non_positive_bar_count_gives_nothing():
    assert random_walk(bars=0) == []
    assert random_walk(bars=-5) == []


# --- summarise ------------------------------------------------------------
def test_summarise_measures_a_known_path():
    stats = summarise(random_walk(bars=1000, seed=3, drift=0.0, volatility=0.02))
    assert isinstance(stats, PathStats)
    assert 0.0 <= stats.max_drawdown < 1.0
    assert stats.terminal_multiple > 0
    assert stats.daily_volatility == pytest.approx(0.02, rel=0.12)
    assert "maxDD" in stats.render()


def test_summarise_needs_a_path():
    with pytest.raises(ValueError, match="at least two bars"):
        summarise(random_walk(bars=1))


# --- CLI wiring -----------------------------------------------------------
class _Args:
    """Stand-in for the parsed argparse namespace."""

    def __init__(self, **kw):
        self.bars = kw.get("bars", 500)
        self.seed = kw.get("seed", 1)
        self.trend = kw.get("trend")
        self.preset = kw.get("preset")


def test_cli_leaves_preset_momentum_alone_by_default():
    """``--trend`` defaults to None, not 0.0, so not passing it must not
    silently flatten the momentum a preset deliberately includes."""
    from brokerbot.cli import _synthetic_kwargs

    kwargs = _synthetic_kwargs(_Args(preset="crypto"))
    assert kwargs["preset"] == "crypto"
    assert "trend_strength" not in kwargs


def test_cli_explicit_trend_overrides_the_preset():
    from brokerbot.cli import _synthetic_kwargs

    assert _synthetic_kwargs(_Args(preset="crypto", trend=0.0))["trend_strength"] == 0.0


def test_cli_noise_baseline_has_no_edge_whatever_the_preset():
    """cmd_noise forces drift and momentum to zero. If a preset's edge leaked
    into the baseline, every strategy would be measured against a bar that
    already contains the thing it is being tested for."""
    from brokerbot.cli import _synthetic_kwargs

    args = _Args(preset="crypto", bars=2000)
    noise = {**_synthetic_kwargs(args), "seed": 0,
             "drift": 0.0, "trend_strength": args.trend or 0.0}
    returns = log_returns(random_walk("NOISE", **noise))

    assert statistics.fmean(returns) == pytest.approx(0.0, abs=3e-3)
    # Crypto's volatility is kept; only the edge is removed.
    assert statistics.pstdev(returns) == pytest.approx(0.035, rel=0.2)
