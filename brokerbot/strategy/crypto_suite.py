"""The strategy suite shown in DaviddTech's "Claude AI Trading Bot" video.

Rebuilt from the names and statistics visible on screen - the Pine Script was
never shown, so these are faithful implementations of the named concepts
rather than copies. Numbers here will not match his exactly, and they are not
meant to: the question is whether the *ideas* survive the checks he applies
plus the two he does not.

His dossier ranked seven strategies on BTC, all long-only on the 4-hour chart
and gated by a bull-regime filter. His stated acceptance criteria, applied
before seeing results:

    at least 100 trades, max drawdown at or under 20%,
    profit factor above 1.1, and it must beat buy-and-hold.

The last one is the one worth holding him to. On ETH his showcased strategy
returned +307% against +1,553% for simply holding ETH, and on BTC +117%
against +937% - both fail his own fourth rule. Only the ADA example cleared
it, and it cleared it because ADA fell 85% while the strategy sat out.

The bull-regime gate is doing a lot of work in all of them, and is the reason
they look good from a March 2020 start: that is the COVID bottom, the single
most flattering entry point available to a long-only crypto system.
"""

from __future__ import annotations

import statistics

from ..models import Bar
from .base import Signal, Strategy


def _sma(values: list[float], n: int) -> float:
    return statistics.fmean(values[-n:]) if len(values) >= n else 0.0


def _ema(values: list[float], n: int) -> float:
    k = 2.0 / (n + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def _atr(bars: list[Bar], n: int) -> float:
    if len(bars) < n + 1:
        return 0.0
    trs = []
    for i in range(len(bars) - n, len(bars)):
        prev = bars[i - 1].close
        trs.append(max(bars[i].high - bars[i].low,
                       abs(bars[i].high - prev), abs(bars[i].low - prev)))
    return statistics.fmean(trs)


class RegimeGated(Strategy):
    """Base: long-only, and only while price is above its regime filter.

    Every strategy in his dossier carries this gate. It is not a detail - on a
    long-only crypto system it is most of the edge, because it is what keeps
    the strategy out of the 2022 collapse. It is also what makes the start
    date matter so much.
    """

    regime_window = 200

    def in_bull(self, closes: list[float]) -> bool:
        if len(closes) < self.regime_window:
            return False
        return closes[-1] > _sma(closes, self.regime_window)


class KalmanVelocity(RegimeGated):
    """Kalman-filtered trend velocity. Long while the estimated slope is up.

    A constant-velocity Kalman filter on closing price gives a smoothed level
    and a rate of change. Trading the sign of the velocity is a trend rule with
    less lag than a moving average, which is the whole appeal.
    """

    name = "kalman_velocity"

    def __init__(self, *, process: float = 1e-4, measure: float = 1e-2,
                 regime: int = 200) -> None:
        super().__init__(process=process, measure=measure, regime=regime)
        self.process, self.measure = process, measure
        self.regime_window = int(regime)

    @property
    def warmup(self) -> int:
        return self.regime_window + 10

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        closes = [b.close for b in history]
        if len(closes) < self.warmup:
            return None

        level, velocity = closes[0], 0.0
        p00 = p01 = p10 = p11 = 1.0
        for z in closes[1:]:
            level += velocity
            p00 += p01 + p10 + p11 + self.process
            p01 += p11
            p10 += p11
            p11 += self.process
            k0 = p00 / (p00 + self.measure)
            k1 = p10 / (p00 + self.measure)
            residual = z - level
            level += k0 * residual
            velocity += k1 * residual
            p00 *= 1 - k0
            p01 *= 1 - k0
            p10 -= k1 * p00
            p11 -= k1 * p01

        if velocity > 0 and self.in_bull(closes):
            return Signal(symbol, 1.0, f"kalman velocity {velocity:+.4f}")
        return Signal(symbol, 0.0, "velocity down or bear regime")


class DonchianBreakout(RegimeGated):
    """Long on a break of the N-bar high, out on a break of the M-bar low."""

    name = "donchian_breakout"

    def __init__(self, *, entry: int = 55, exit: int = 20, regime: int = 200) -> None:
        super().__init__(entry=entry, exit=exit, regime=regime)
        self.entry, self.exit = int(entry), int(exit)
        self.regime_window = int(regime)

    @property
    def warmup(self) -> int:
        return max(self.entry, self.regime_window) + 2

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) < self.warmup:
            return None
        closes = [b.close for b in history]
        # Prior bars only: comparing today's close to a high that includes
        # today is trivially satisfied and would trade every bar.
        high = max(b.high for b in history[-self.entry - 1:-1])
        low = min(b.low for b in history[-self.exit - 1:-1])
        if closes[-1] > high and self.in_bull(closes):
            return Signal(symbol, 1.0, f"broke {self.entry}-bar high")
        if closes[-1] < low:
            return Signal(symbol, 0.0, f"broke {self.exit}-bar low")
        return None


class SqueezeBreakout(RegimeGated):
    """Bollinger bands inside Keltner channels, then break out of the squeeze."""

    name = "squeeze_breakout"

    def __init__(self, *, window: int = 20, bb: float = 2.0, kc: float = 1.5,
                 regime: int = 200) -> None:
        super().__init__(window=window, bb=bb, kc=kc, regime=regime)
        self.window, self.bb, self.kc = int(window), bb, kc
        self.regime_window = int(regime)

    @property
    def warmup(self) -> int:
        return max(self.window, self.regime_window) + 2

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) < self.warmup:
            return None
        closes = [b.close for b in history]
        mid = _sma(closes, self.window)
        sd = statistics.pstdev(closes[-self.window:])
        atr = _atr(history, self.window)
        if sd <= 0 or atr <= 0:
            return None
        squeezed = (mid + self.bb * sd) < (mid + self.kc * atr)
        if squeezed:
            return None
        if closes[-1] > mid + self.bb * sd and self.in_bull(closes):
            return Signal(symbol, 1.0, "squeeze released upward")
        if closes[-1] < mid:
            return Signal(symbol, 0.0, "back below the mean")
        return None


class ObvVolumeTrend(RegimeGated):
    """On-balance volume above its own average: volume confirming the trend."""

    name = "obv_volume_trend"

    def __init__(self, *, window: int = 30, regime: int = 200) -> None:
        super().__init__(window=window, regime=regime)
        self.window = int(window)
        self.regime_window = int(regime)

    @property
    def warmup(self) -> int:
        return max(self.window, self.regime_window) + 2

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) < self.warmup:
            return None
        obv, series = 0.0, []
        for i in range(1, len(history)):
            if history[i].close > history[i - 1].close:
                obv += history[i].volume
            elif history[i].close < history[i - 1].close:
                obv -= history[i].volume
            series.append(obv)
        if len(series) < self.window:
            return None
        closes = [b.close for b in history]
        if series[-1] > _sma(series, self.window) and self.in_bull(closes):
            return Signal(symbol, 1.0, "OBV above its average")
        return Signal(symbol, 0.0, "OBV rolling over")


class EmaRegime(RegimeGated):
    """Fast over slow EMA, gated by the regime filter."""

    name = "ema_regime"

    def __init__(self, *, fast: int = 21, slow: int = 55, regime: int = 200) -> None:
        super().__init__(fast=fast, slow=slow, regime=regime)
        self.fast, self.slow = int(fast), int(slow)
        self.regime_window = int(regime)

    @property
    def warmup(self) -> int:
        return max(self.slow, self.regime_window) + 2

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) < self.warmup:
            return None
        closes = [b.close for b in history]
        if _ema(closes[-self.slow * 3:], self.fast) > _ema(closes[-self.slow * 3:], self.slow) \
                and self.in_bull(closes):
            return Signal(symbol, 1.0, "fast EMA above slow")
        return Signal(symbol, 0.0, "fast EMA below slow")


class GaussianDeadband(RegimeGated):
    """Gaussian-weighted average with a deadband, so noise does not flip it."""

    name = "gaussian_deadband"

    def __init__(self, *, window: int = 30, deadband: float = 0.01,
                 regime: int = 200) -> None:
        super().__init__(window=window, deadband=deadband, regime=regime)
        self.window, self.deadband = int(window), deadband
        self.regime_window = int(regime)

    @property
    def warmup(self) -> int:
        return max(self.window, self.regime_window) + 2

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) < self.warmup:
            return None
        closes = [b.close for b in history]
        window = closes[-self.window:]
        sigma = self.window / 4.0
        mid = (self.window - 1) / 2.0
        weights = [2.71828 ** (-((i - mid) ** 2) / (2 * sigma ** 2))
                   for i in range(self.window)]
        total = sum(weights)
        smooth = sum(w * v for w, v in zip(weights, window)) / total

        # The deadband is the point: without it, price hovering on the line
        # produces a trade every bar and the costs eat the strategy alive.
        if closes[-1] > smooth * (1 + self.deadband) and self.in_bull(closes):
            return Signal(symbol, 1.0, "above the band")
        if closes[-1] < smooth * (1 - self.deadband):
            return Signal(symbol, 0.0, "below the band")
        return None


class RankConsensus(RegimeGated):
    """Only long when a majority of the other rules agree.

    His second-ranked strategy. Consensus is a real idea - independent rules
    agreeing is worth more than one rule shouting - but it is also the rule
    most likely to be fitted, since 'how many must agree' is a free parameter
    chosen after seeing the answer.
    """

    name = "rank_consensus"

    def __init__(self, *, threshold: int = 3, regime: int = 200) -> None:
        super().__init__(threshold=threshold, regime=regime)
        self.threshold = int(threshold)
        self.regime_window = int(regime)
        self.members = [KalmanVelocity(regime=regime), DonchianBreakout(regime=regime),
                        EmaRegime(regime=regime), ObvVolumeTrend(regime=regime),
                        GaussianDeadband(regime=regime)]

    @property
    def warmup(self) -> int:
        return max(m.warmup for m in self.members)

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) < self.warmup:
            return None
        votes = 0
        for m in self.members:
            s = m.on_bar(symbol, history)
            if s is not None and s.target_weight > 0.5:
                votes += 1
        if votes >= self.threshold and self.in_bull([b.close for b in history]):
            return Signal(symbol, 1.0, f"{votes} of {len(self.members)} agree")
        return Signal(symbol, 0.0, f"only {votes} agree")


SUITE: dict[str, type[Strategy]] = {
    "kalman_velocity": KalmanVelocity,
    "rank_consensus": RankConsensus,
    "donchian_breakout": DonchianBreakout,
    "squeeze_breakout": SqueezeBreakout,
    "obv_volume_trend": ObvVolumeTrend,
    "ema_regime": EmaRegime,
    "gaussian_deadband": GaussianDeadband,
}
