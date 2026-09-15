"""Strategy interface.

A strategy sees bars one at a time and returns target positions. The interface
is deliberately restrictive in one specific way: :meth:`on_bar` receives only
the bars up to and including the current one. It is structurally impossible to
read tomorrow's price, because the engine never hands it over.

That constraint exists because look-ahead bias is the single most common
reason a backtest looks brilliant and then loses money live, and it is very
easy to introduce by accident when a strategy is handed a whole DataFrame and
trusted to only look leftwards.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import Bar


@dataclass(slots=True)
class Signal:
    """A desired position, expressed as a fraction of portfolio equity.

    ``target_weight`` of 1.0 means fully invested, 0.0 means flat. Fractions
    rather than share counts, so the same strategy works at any account size
    and position sizing stays the engine's job.
    """

    symbol: str
    target_weight: float
    reason: str = ""

    def __post_init__(self) -> None:
        # Long-only by default. Shorting through a retail broker has borrow
        # costs and assignment risk this framework does not model, so a
        # strategy that wants it must be explicit rather than arriving there
        # by an arithmetic slip.
        self.target_weight = max(0.0, min(1.0, self.target_weight))


class Strategy(ABC):
    name: str = "strategy"

    def __init__(self, **params: float) -> None:
        self.params = params

    @abstractmethod
    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        """Called once per bar per symbol.

        ``history`` ends at the current bar. Returning ``None`` means "no
        change" - the existing position is left alone.
        """

    @property
    def warmup(self) -> int:
        """Bars needed before the strategy can produce a signal.

        The engine skips these, so an indicator is never computed on a partial
        window and then traded on.
        """
        return 0

    def describe(self) -> str:
        params = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.name}({params})" if params else self.name
