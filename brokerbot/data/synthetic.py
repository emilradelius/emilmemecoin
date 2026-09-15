"""Synthetic price series for calibration.

This module exists to answer one question: *what does my strategy score on
data that contains no edge at all?* Run it on a few hundred random walks and
the spread of returns you get back is the performance the method manufactures
from nothing. A real backtest only means something if it stands clearly
outside that spread.

That baseline is only trustworthy if the synthetic paths resemble the asset
class being tested, which is why the generator is parameterised rather than
fixed, and why :data:`ASSET_PRESETS` ships calibrated settings for the two
classes people actually point it at.

Orthogonality
-------------
``drift``, ``volatility`` and ``trend_strength`` are independent. Changing one
does not move the others. That property is load-bearing - it is what lets you
ask "what does 3.5% daily volatility do to this strategy?" without silently
changing the expected return at the same time - and it is easy to break. Two
bugs that did break it, both fixed here, are worth naming so they are not
reintroduced:

**1. Drift amplification through the momentum accumulator.** If the momentum
accumulator is fed the *total* per-bar return, it accumulates the drift term
as well as the noise. ``trend_strength`` then multiplies the drift, and a run
with trend enabled shows a higher realised return than the same ``drift``
without it. The accumulator here is fed only the standardised noise
innovations (``eps``), never the drift term, so trend and drift cannot mix.

**2. Variance-driven geometric-return loss.** Compounding simple returns drawn
as ``drift + volatility * z`` gives a realised geometric growth rate of about
``drift - volatility**2 / 2``: raise volatility and the path drifts downwards
even though ``drift`` never changed. The walk here is built in log space with
a mean log return of exactly ``log1p(drift)``, so the median terminal price is
``start_price * (1 + drift) ** bars`` for any volatility at all.

Both properties are asserted in ``tests/brokerbot/test_synthetic.py``.

Fat tails
---------
Gaussian returns are fine for a moderate-volatility equity index and wrong for
crypto. At the ~3.5% daily volatility of a real crypto asset, compounded
Gaussian returns diverge: buy-and-hold drawdowns reach 100% (the price goes
effectively to zero) and median terminal multiples swing across orders of
magnitude between seeds. Real Bitcoin's worst drawdown over 2015-2025 was
about -83%.

``distribution="student_t"`` puts the excess kurtosis in the individual
returns instead of in the compounded path, which is where real assets keep it.
Combined with ``max_move`` (exchanges halt; circuit breakers fire) and
``vol_clustering`` (volatility arrives in bursts rather than at a constant
level), it produces paths whose *shape* matches the asset class.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models import Bar

__all__ = ["random_walk", "ASSET_PRESETS", "PathStats", "summarise", "calibrate"]


class _Unset:
    """Sentinel: 'caller said nothing', distinct from an explicit ``None``.

    ``max_move=None`` is a meaningful value (no cap), so a plain ``None``
    default cannot tell "leave it to the preset" from "turn the cap off".
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()

#: Baseline parameters, used when neither the caller nor a preset supplies one.
#: A moderate-volatility equity: ~1.2% daily (19% annualised), mild positive
#: drift, Gaussian returns, no cap, no clustering.
_BASE: dict[str, object] = {
    "start_price": 100.0,
    "drift": 0.0003,
    "volatility": 0.012,
    "trend_strength": 0.0,
    "distribution": "normal",
    "df": 4.0,
    "max_move": None,
    "vol_clustering": 0.0,
}

#: Parameter sets that reproduce the *shape* of a real asset class.
#:
#: Calibrated over 2,500 bars (about ten trading years) against the bands in
#: ``_CALIBRATION_TARGETS`` below: terminal multiple, buy-and-hold maximum
#: drawdown, and realised daily volatility. See ``calibrate()`` to re-check
#: them, and the noise-calibration section of ``brokerbot/README.md``.
ASSET_PRESETS: dict[str, dict[str, object]] = {
    # Broad equity index: low drift, ~1.1% daily (17% annualised), mildly fat
    # tails. Ten-year drawdowns in the 30-55% band - 2008 was -57%, 2020 was
    # -34%.
    "equity_index": {
        "drift": 0.00035,
        "volatility": 0.011,
        "trend_strength": 0.25,
        "distribution": "student_t",
        "df": 5.0,
        "max_move": 0.13,
        "vol_clustering": 0.45,
    },
    # Single large-cap equity: more volatile than the index it sits in, and
    # without the index's diversification floor under its drawdowns.
    "single_equity": {
        "drift": 0.0004,
        "volatility": 0.019,
        "trend_strength": 0.30,
        "distribution": "student_t",
        "df": 4.0,
        "max_move": 0.20,
        "vol_clustering": 0.45,
    },
    # Crypto: ~3.5% daily (55% annualised) with the strong drift that has to
    # accompany it for the asset to survive its own drawdowns. Bitcoin ran
    # about 267x over 2015-2025 (~2,500 trading days) with a worst drawdown
    # near -83%; this lands near 210x and -76%.
    "crypto": {
        "drift": 0.0022,
        "volatility": 0.035,
        "trend_strength": 0.45,
        "distribution": "student_t",
        "df": 4.0,
        "max_move": 0.30,
        "vol_clustering": 0.55,
    },
    # Zero-edge walk for noise calibration at crypto volatility.
    #
    # Read this before using it. The three presets above deliberately carry
    # trend_strength > 0, because serial correlation is part of the shape they
    # reproduce: a decade-long path cannot show both crypto's terminal
    # multiple and crypto's -80% drawdowns without bear markets that persist.
    # But momentum is a real, exploitable edge, so those presets are NOT a
    # zero-edge baseline - a trend-following strategy is supposed to make
    # money on them.
    #
    # This one has drift 0.0 and trend_strength 0.0: an efficient market with
    # crypto's volatility, fat tails and clustering, and nothing to find. It
    # is what a strategy should be scored against, and what the ``noise``
    # subcommand needs.
    "crypto_noise": {
        "drift": 0.0,
        "volatility": 0.035,
        "trend_strength": 0.0,
        "distribution": "student_t",
        "df": 4.0,
        "max_move": 0.30,
        "vol_clustering": 0.55,
    },
}

#: Bands each preset is expected to land in, as (low, high) on the *median*
#: across seeds over 2,500 bars. Asserted in the test suite; a change to the
#: generator that pushes a preset outside its band is a regression.
_CALIBRATION_TARGETS: dict[str, dict[str, tuple[float, float]]] = {
    "equity_index": {
        "terminal_multiple": (1.4, 4.5),
        "max_drawdown": (0.30, 0.55),
        "daily_volatility": (0.009, 0.013),
    },
    "single_equity": {
        "terminal_multiple": (1.4, 6.0),
        "max_drawdown": (0.45, 0.75),
        "daily_volatility": (0.016, 0.022),
    },
    "crypto": {
        "terminal_multiple": (40.0, 900.0),
        "max_drawdown": (0.70, 0.85),
        "daily_volatility": (0.030, 0.040),
    },
}

# Persistence of the log-volatility process. 0.94 is the usual daily figure:
# a volatility burst decays over a few weeks, not a few days.
_VOL_PERSISTENCE = 0.94

# Decay of the momentum accumulator. 0.9 gives a trend memory of about ten
# bars, short enough that a crossover strategy can actually catch it.
_MOMENTUM_DECAY = 0.9


def _resolve(name: str, given: object, preset: dict[str, object]) -> object:
    """Explicit argument beats preset beats baseline."""
    if not isinstance(given, _Unset):
        return given
    if name in preset:
        return preset[name]
    return _BASE[name]


def _standard_innovation(rng: random.Random, distribution: str, df: float) -> float:
    """Draw a shock with mean 0 and variance exactly 1.

    Standardising here is what keeps ``volatility`` meaning "the standard
    deviation of log returns" whatever distribution is selected. Swap normal
    for Student-t and the tails get heavier while the realised volatility
    stays put, which is the whole point of the option.
    """
    if distribution == "normal":
        return rng.gauss(0.0, 1.0)

    if distribution == "student_t":
        # t_df = z / sqrt(chi2_df / df), and Var(t_df) = df / (df - 2), so
        # scaling by sqrt((df - 2) / df) brings it back to unit variance.
        chi2 = rng.gammavariate(df / 2.0, 2.0)
        raw = rng.gauss(0.0, 1.0) / math.sqrt(chi2 / df)
        return raw * math.sqrt((df - 2.0) / df)

    raise ValueError(
        f"unknown distribution {distribution!r}; expected 'normal' or 'student_t'"
    )


def random_walk(
    symbol: str = "SYNTH",
    *,
    bars: int = 1500,
    seed: int = 1,
    start: datetime | None = None,
    start_price: float | _Unset = _UNSET,
    drift: float | _Unset = _UNSET,
    volatility: float | _Unset = _UNSET,
    trend_strength: float | _Unset = _UNSET,
    distribution: str | _Unset = _UNSET,
    df: float | _Unset = _UNSET,
    max_move: float | None | _Unset = _UNSET,
    vol_clustering: float | _Unset = _UNSET,
    preset: str | None = None,
) -> list[Bar]:
    """Generate a synthetic daily series.

    :param bars: number of trading days (weekends are skipped).
    :param seed: fixes the path; the same seed always gives the same series.
    :param start_price: first close. Default 100.
    :param drift: expected *geometric* return per bar. ``0.0`` is an efficient
        market with no edge to find. The median terminal price is
        ``start_price * (1 + drift) ** bars`` regardless of volatility.
    :param volatility: standard deviation of log returns per bar. Default
        0.012 (~19% annualised). Crypto is nearer 0.035.
    :param trend_strength: how much recent noise feeds the next return, giving
        the series momentum. ``0.0`` is an efficient market. Does not change
        the realised drift or volatility at any setting.
    :param distribution: ``"normal"`` or ``"student_t"``. Student-t has the
        fat tails of real daily returns without the compounded path
        diverging; use it for anything high-volatility.
    :param df: degrees of freedom for Student-t. Must be > 2 for the variance
        to exist; ~4 matches daily asset returns.
    :param max_move: cap on a single bar's move, as a fraction (``0.30`` =
        30%). Real venues halt trading; uncapped fat tails do not. ``None``
        disables the cap. Applied symmetrically in log space to the *noise*
        term, so a 0.30 cap allows +30% up and -23% down, plus the (tiny)
        drift term. Clamping the total return instead would make a binding
        cap asymmetric and bleed away the drift.
    :param vol_clustering: standard deviation of log-volatility. ``0.0`` holds
        volatility constant; higher values make it arrive in bursts, as it
        does in real markets. The *average* variance stays ``volatility**2``.
    :param preset: a key of :data:`ASSET_PRESETS` supplying calibrated
        defaults for the above. Explicit arguments still win.

    Defaults are unchanged from the original Gaussian generator: calling
    ``random_walk(bars=200, seed=1)`` gives the same series it always did.
    """
    if preset is not None and preset not in ASSET_PRESETS:
        raise ValueError(
            f"unknown preset {preset!r}; expected one of {sorted(ASSET_PRESETS)}"
        )
    chosen = ASSET_PRESETS.get(preset, {}) if preset else {}

    price = float(_resolve("start_price", start_price, chosen))
    mu = float(_resolve("drift", drift, chosen))
    sigma = float(_resolve("volatility", volatility, chosen))
    trend = float(_resolve("trend_strength", trend_strength, chosen))
    dist = str(_resolve("distribution", distribution, chosen))
    dof = float(_resolve("df", df, chosen))
    cap = _resolve("max_move", max_move, chosen)
    clustering = float(_resolve("vol_clustering", vol_clustering, chosen))

    if bars <= 0:
        return []
    if price <= 0:
        raise ValueError("start_price must be positive")
    if sigma < 0:
        raise ValueError("volatility cannot be negative")
    if dist == "student_t" and dof <= 2.0:
        raise ValueError("student_t needs df > 2 for its variance to exist")
    if cap is not None and float(cap) <= 0:
        raise ValueError("max_move must be positive, or None for no cap")
    if clustering < 0:
        raise ValueError("vol_clustering cannot be negative")

    rng = random.Random(seed)
    ts = start or datetime(2020, 1, 1)

    # --- orthogonality bookkeeping ---------------------------------------
    # The mean log return is log1p(drift) exactly, so the median terminal
    # price is start_price * (1 + drift) ** bars for any volatility. Adding
    # the drift in log space is what avoids the variance-driven geometric
    # loss described in the module docstring.
    mu_log = math.log1p(mu)

    # The momentum accumulator is an EWMA of past standardised innovations,
    # so Var(momentum) = (1 - lam) / (1 + lam). Dividing the combined shock by
    # sqrt(1 + trend**2 * Var(momentum)) holds the total variance at exactly
    # 1, which is what keeps trend_strength from leaking into volatility.
    lam = _MOMENTUM_DECAY
    momentum_var = (1.0 - lam) / (1.0 + lam)
    trend_norm = math.sqrt(1.0 + trend * trend * momentum_var)
    momentum = 0.0

    # Log-volatility is a mean-reverting AR(1). Subtracting its stationary
    # variance normalises E[sigma_t**2] back to sigma**2, so clustering
    # redistributes volatility through time without adding any.
    log_vol_var = clustering * clustering
    log_vol_shock = clustering * math.sqrt(1.0 - _VOL_PERSISTENCE**2)
    log_vol = rng.gauss(0.0, clustering) if clustering > 0 else 0.0

    cap_log = math.log1p(float(cap)) if cap is not None else None

    out: list[Bar] = []
    for _ in range(bars):
        # Volatility for this bar, drawn before the return so it depends only
        # on the past - a burst has to be under way before it can affect a
        # price, as it is in a real market.
        if clustering > 0:
            sigma_t = sigma * math.exp(log_vol - log_vol_var)
        else:
            sigma_t = sigma

        eps = _standard_innovation(rng, dist, dof)

        # Combine noise with momentum built ONLY from past noise. Feeding the
        # drift in here is bug #1 from the module docstring.
        combined = (eps + trend * momentum) / trend_norm
        momentum = lam * momentum + (1.0 - lam) * eps

        shock = sigma_t * combined
        if cap_log is not None:
            # Symmetric clamp of a symmetric distribution: the mean stays 0,
            # so the cap trims the tails without touching the drift.
            shock = max(-cap_log, min(cap_log, shock))

        previous = price
        price = previous * math.exp(mu_log + shock)

        # Intrabar range, scaled to this bar's volatility. Built outwards from
        # the open/close pair so low <= open, close <= high always holds and
        # the bar can never fail its own validator.
        reach = sigma_t * 0.7
        high = max(previous, price) * (1.0 + rng.random() * reach)
        low = min(previous, price) * (1.0 - rng.random() * reach)
        volume = round(rng.uniform(5e5, 2e6), 2)

        out.append(Bar(symbol, ts, previous, high, low, price, volume))

        ts += timedelta(days=1)
        while ts.weekday() >= 5:  # trading days only
            ts += timedelta(days=1)

        if clustering > 0:
            log_vol = _VOL_PERSISTENCE * log_vol + rng.gauss(0.0, log_vol_shock)

    return out


# --- calibration ----------------------------------------------------------
@dataclass(frozen=True)
class PathStats:
    """Shape statistics for one generated path."""

    terminal_multiple: float
    max_drawdown: float
    daily_volatility: float
    daily_drift: float

    def render(self) -> str:
        return (
            f"terminal {self.terminal_multiple:,.2f}x  "
            f"maxDD {self.max_drawdown:.1%}  "
            f"daily vol {self.daily_volatility:.2%}"
        )


def summarise(bars: list[Bar]) -> PathStats:
    """Measure the shape of a generated path.

    Buy-and-hold drawdown, terminal multiple and realised daily volatility are
    the three numbers that decide whether a synthetic series resembles the
    asset class you are about to test a strategy on.
    """
    if len(bars) < 2:
        raise ValueError("need at least two bars to summarise a path")

    closes = [b.close for b in bars]
    first, last = closes[0], closes[-1]

    peak = closes[0]
    worst = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak > 0:
            worst = max(worst, 1.0 - close / peak)

    log_returns = [
        math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0
    ]

    return PathStats(
        terminal_multiple=last / first,
        max_drawdown=worst,
        daily_volatility=statistics.pstdev(log_returns) if len(log_returns) > 1 else 0.0,
        daily_drift=statistics.fmean(log_returns) if log_returns else 0.0,
    )


def calibrate(
    preset: str | None = None,
    *,
    runs: int = 60,
    bars: int = 2500,
    **overrides,
) -> dict[str, float]:
    """Median path statistics across ``runs`` seeds.

    The generator is stochastic, so a single path proves nothing about the
    settings that produced it. This runs a batch and reports medians, which is
    what :data:`_CALIBRATION_TARGETS` is stated in and what the test suite
    checks::

        >>> stats = calibrate("crypto", runs=40)
        >>> 0.70 <= stats["max_drawdown"] <= 0.85
        True
    """
    paths = [
        summarise(random_walk(bars=bars, seed=seed, preset=preset, **overrides))
        for seed in range(runs)
    ]
    return {
        "terminal_multiple": statistics.median(p.terminal_multiple for p in paths),
        "max_drawdown": statistics.median(p.max_drawdown for p in paths),
        "daily_volatility": statistics.median(p.daily_volatility for p in paths),
        "daily_drift": statistics.median(p.daily_drift for p in paths),
        "worst_drawdown": max(p.max_drawdown for p in paths),
    }
