"""Post-news drift strategy.

**The premise, stated plainly: you cannot win the speed race, so do not
enter it.** Institutional systems receive machine-readable news over WebSocket
in roughly 25 milliseconds. By the time a story reaches a public RSS feed, the
first move is over and whoever traded it was faster than you will ever be. A
retail bot racing to react to headlines is systematically buying from people
with better information and better latency.

What *is* available at retail speed is **post-earnings-announcement drift** -
the tendency of prices to keep moving in the direction of an earnings surprise
for weeks afterwards. It is among the most replicated anomalies in finance,
precisely because it plays out over a horizon slow enough to be reachable.

So this strategy deliberately does the opposite of what a news bot is expected
to do:

1. A material, novel, directional story arrives. **It does not trade.**
2. It waits for the market to confirm - price must move in the signal's
   direction by a threshold. A story the market shrugs at was not news.
3. Only then does it enter, and it holds for a fixed drift window measured in
   days, not minutes.

The confirmation step is what makes this robust to the classifier being wrong.
A misclassified story produces no confirming move, so no trade follows. The
market gets a veto over the model.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from ..models import Bar
from ..news.models import Direction, NewsSignal
from .base import Signal, Strategy

log = logging.getLogger(__name__)


class NewsDriftStrategy(Strategy):
    name = "news_drift"

    def __init__(
        self,
        signals: list[NewsSignal] | None = None,
        *,
        min_score: float = 0.35,
        confirmation_pct: float = 0.01,
        confirmation_bars: int = 2,
        hold_days: int = 20,
        stop_loss_pct: float = 0.08,
        signal_ttl_days: int = 3,
    ) -> None:
        super().__init__(
            min_score=min_score, confirmation_pct=confirmation_pct,
            hold_days=hold_days, stop_loss_pct=stop_loss_pct,
        )
        self.min_score = min_score
        self.confirmation_pct = confirmation_pct
        self.confirmation_bars = confirmation_bars
        self.hold_days = hold_days
        self.stop_loss_pct = stop_loss_pct
        self.signal_ttl = timedelta(days=signal_ttl_days)

        self._by_symbol: dict[str, list[NewsSignal]] = defaultdict(list)
        for s in signals or []:
            self.add_signal(s)

        self._entry: dict[str, tuple[datetime, float]] = {}

    def add_signal(self, signal: NewsSignal) -> None:
        self._by_symbol[signal.symbol].append(signal)
        self._by_symbol[signal.symbol].sort(key=lambda s: s.ts)

    @property
    def warmup(self) -> int:
        return self.confirmation_bars + 1

    def _visible_signals(self, symbol: str, now: datetime) -> list[NewsSignal]:
        """Signals our ingester had already seen by ``now``.

        Keyed on ``first_seen_at``, never ``published_at`` - see
        :mod:`brokerbot.news.models` for why publisher timestamps cannot be
        trusted in a backtest.
        """
        return [
            s for s in self._by_symbol.get(symbol, [])
            if s.ts <= now and (now - s.ts) <= self.signal_ttl
            and s.score >= self.min_score
        ]

    def on_bar(self, symbol: str, history: list[Bar]) -> Signal | None:
        if len(history) < self.warmup:
            return None
        bar = history[-1]
        now = bar.ts

        # --- manage an open position -------------------------------------
        held = self._entry.get(symbol)
        if held is not None:
            entry_ts, entry_price = held
            days_held = (now - entry_ts).days
            if entry_price > 0 and bar.close / entry_price - 1.0 <= -self.stop_loss_pct:
                self._entry.pop(symbol, None)
                return Signal(symbol, 0.0, "stop loss")
            if days_held >= self.hold_days:
                self._entry.pop(symbol, None)
                return Signal(symbol, 0.0, f"drift window closed ({days_held}d)")
            return None

        # --- look for a confirmed entry ----------------------------------
        live = self._visible_signals(symbol, now)
        if not live:
            return None
        best = max(live, key=lambda s: s.score)
        if best.assessment.direction is not Direction.BULLISH:
            # Long-only: a bearish story is a reason not to hold, not to short.
            return None

        # Confirmation: has the market moved in the signal's direction since
        # the story became visible to us? Without this the strategy is just
        # trusting the classifier, and a misread headline becomes a position.
        reference = self._price_at_or_before(history, best.ts)
        if reference is None or reference <= 0:
            return None
        move = bar.close / reference - 1.0
        if move < self.confirmation_pct:
            return None

        self._entry[symbol] = (now, bar.close)
        return Signal(
            symbol, 1.0,
            f"{best.assessment.event_type.value} confirmed "
            f"({move:+.1%} since the story, score {best.score:.2f})",
        )

    @staticmethod
    def _price_at_or_before(history: list[Bar], ts: datetime) -> float | None:
        chosen = None
        for bar in history:
            if bar.ts <= ts:
                chosen = bar.close
            else:
                break
        return chosen
