"""Bar sources and the data-quality validator.

The validator exists because bad price data does not produce an obvious
error - it produces a confident, wrong backtest. An unadjusted split looks
like a 50% crash the strategy "correctly" avoided; a close printed outside
its own high/low range quietly lets the engine fill at a price that never
traded. Both make a report that looks entirely normal.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Bar

# A single-bar move beyond these ratios is not a market move. Adjusted daily
# equity and crypto series do not gap 45% down or 80% up in one bar; share
# splits and unadjusted dividends do exactly that, which is the point.
SPLIT_DROP_RATIO = 0.55
SPLIT_RISE_RATIO = 1.8


class BarSource(ABC):
    """Anything that can produce a list of bars for a symbol."""

    name: str = "source"

    @abstractmethod
    def load(self, symbol: str, **kwargs) -> list[Bar]:
        """Return bars for ``symbol``, oldest first."""

    @staticmethod
    def validate(bars: list[Bar]) -> list[str]:
        """Return a list of human-readable problems. Empty means clean.

        Deliberately returns warnings rather than raising: the caller decides
        whether a flaw is fatal. What it must never do is stay silent.
        """
        if not bars:
            return ["empty series"]

        problems: list[str] = []
        previous: Bar | None = None

        for i, bar in enumerate(bars):
            if previous is not None and bar.ts <= previous.ts:
                problems.append(
                    f"bar {i}: non-monotonic timestamp {bar.ts} follows {previous.ts}"
                )

            if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                problems.append(f"bar {i} at {bar.ts}: non-positive price")
                previous = bar
                continue

            if bar.high < bar.low:
                problems.append(f"bar {i} at {bar.ts}: high below low")
                previous = bar
                continue

            if not (bar.low <= bar.open <= bar.high):
                problems.append(f"bar {i} at {bar.ts}: open outside high/low range")
            if not (bar.low <= bar.close <= bar.high):
                problems.append(f"bar {i} at {bar.ts}: close outside high/low range")

            if bar.volume < 0:
                problems.append(f"bar {i} at {bar.ts}: negative volume")

            if previous is not None and previous.close > 0:
                ratio = bar.close / previous.close
                if ratio <= SPLIT_DROP_RATIO or ratio >= SPLIT_RISE_RATIO:
                    problems.append(
                        f"bar {i} at {bar.ts}: price moved {previous.close:g} -> "
                        f"{bar.close:g} in one bar - suspected unadjusted split "
                        f"or bad print. Use split- and dividend-adjusted prices."
                    )

            previous = bar

        return problems
