"""Reference strategies.

These are textbook strategies, included so there is something concrete to
backtest and, more importantly, to measure against buy-and-hold. They are
starting points for your own work, not recommendations.

Worth knowing before you get attached to any of them: these are among the most
widely published and most heavily arbitraged rules in existence. If a moving
average crossover on a liquid large-cap reliably beat holding it, that would
have been competed away decades ago. The value of running them is calibration
- seeing what a real result looks like after costs, so you can recognise
whether your own ideas are actually better or merely untested.
"""

from __future__ import annotations

import statistics

from ..models import Bar
from .base import Signal, Strategy


def _sma(bars: list[Bar], window: int) -> float | None:
    if len(bars) < window:
        return None
    return statistics.fmean(b.close for b in bars[-window:])


class BuyAndHold(Strategy):
    """Buy once, never sell. The benchmark every strategy must beat."""

    name = "buy_and_hold"

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) == 1:
            return Signal(symbol, 1.0, "initial entry")
        return None


class SmaCrossover(Strategy):
    """Long when the fast average is above the slow one, flat otherwise."""

    name = "sma_crossover"

    def __init__(self, fast: int = 50, slow: int = 200) -> None:
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        super().__init__(fast=fast, slow=slow)
        self.fast = int(fast)
        self.slow = int(slow)

    @property
    def warmup(self) -> int:
        return self.slow

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        fast = _sma(history, self.fast)
        slow = _sma(history, self.slow)
        if fast is None or slow is None:
            return None
        if fast > slow:
            return Signal(symbol, 1.0, f"SMA{self.fast} above SMA{self.slow}")
        return Signal(symbol, 0.0, f"SMA{self.fast} below SMA{self.slow}")


class Momentum(Strategy):
    """Long while trailing return over ``lookback`` bars is positive.

    Time-series momentum is one of the few anomalies with decades of
    out-of-sample evidence behind it. That does not mean it works net of
    retail costs at retail frequency, which is exactly what the backtest is
    for.
    """

    name = "momentum"

    def __init__(self, lookback: int = 126, threshold: float = 0.0) -> None:
        super().__init__(lookback=lookback, threshold=threshold)
        self.lookback = int(lookback)
        self.threshold = float(threshold)

    @property
    def warmup(self) -> int:
        return self.lookback + 1

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) <= self.lookback:
            return None
        past = history[-self.lookback - 1].close
        if past <= 0:
            return None
        trailing = history[-1].close / past - 1.0
        if trailing > self.threshold:
            return Signal(symbol, 1.0, f"{self.lookback}-bar return {trailing:+.1%}")
        return Signal(symbol, 0.0, f"{self.lookback}-bar return {trailing:+.1%}")


class MeanReversion(Strategy):
    """Buy dips below a moving average, exit on reversion to it.

    Included as a deliberate contrast: mean reversion trades far more often
    than the trend strategies, which makes it the clearest demonstration of
    how costs decide outcomes. Compare its gross and net results.
    """

    name = "mean_reversion"

    def __init__(self, window: int = 20, entry_z: float = -1.5,
                 exit_z: float = 0.0) -> None:
        super().__init__(window=window, entry_z=entry_z, exit_z=exit_z)
        self.window = int(window)
        self.entry_z = float(entry_z)
        self.exit_z = float(exit_z)

    @property
    def warmup(self) -> int:
        return self.window

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) < self.window:
            return None
        window = [b.close for b in history[-self.window:]]
        mean = statistics.fmean(window)
        sd = statistics.pstdev(window)
        if sd <= 0:
            return None
        z = (history[-1].close - mean) / sd
        if z <= self.entry_z:
            return Signal(symbol, 1.0, f"z={z:.2f} below entry")
        if z >= self.exit_z:
            return Signal(symbol, 0.0, f"z={z:.2f} reverted")
        return None


from .news_drift import NewsDriftStrategy  # noqa: E402

REGISTRY: dict[str, type[Strategy]] = {
    "buy_and_hold": BuyAndHold,
    "news_drift": NewsDriftStrategy,
    "sma_crossover": SmaCrossover,
    "momentum": Momentum,
    "mean_reversion": MeanReversion,
}
