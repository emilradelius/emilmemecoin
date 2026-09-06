"""DexScreener flow analysis: is the move still live, or already over?

**Why this is not a fourth consensus source.** DexScreener tells you *what* is
happening to a token - price, volume, how many buys and sells - but never
*who* is doing it. The consensus engine counts independent, individually-scored
actors, and anonymous aggregate volume has no actor to attribute. Treating it
as a source would mean counting a pump-and-dump's own wash volume as if it
were a good trader's opinion, which inflates conviction on precisely the
tokens you most want to avoid.

So flow data does a different, complementary job: it **confirms or vetoes**
what the traders are telling you. Three questions it can answer that the
trader signals cannot:

1. **Is the move still happening?** Signals arrive with lag. A token where
   five wallets bought twenty minutes ago but current 5-minute volume has
   collapsed is a move you already missed. Buying into that is buying their
   exit.

2. **Who is winning right now?** The buy/sell transaction split over the last
   five minutes says whether the current tape is accumulation or
   distribution, regardless of what happened an hour ago.

3. **Has it already run?** A token up 400% in an hour and decelerating is
   late-stage. The asymmetry that made it worth buying is gone, and what is
   left is mostly downside.

The output is a multiplier applied to conviction, plus hard vetoes for the
unambiguous cases. It can only ever *reduce* an alert's strength or block it -
flow data alone can never promote something to STRONG, because there is no
one behind it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .dexscreener import MarketData

log = logging.getLogger(__name__)


@dataclass(slots=True)
class FlowAnalysis:
    mint: str

    buy_pressure_5m: float | None = None
    """Share of last-5-minute transactions that were buys, 0-1."""

    buy_pressure_1h: float | None = None
    acceleration: float | None = None
    """Current 5-minute volume rate against the trailing hourly rate. >1 means
    volume is picking up, <1 means it is draining away."""

    run_up_1h: float | None = None
    momentum_score: float = 0.5
    """Composite 0-1. 0.5 is neutral (unknown or unremarkable)."""

    multiplier: float = 1.0
    veto: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.veto is not None


class FlowAnalyzer:
    def __init__(
        self,
        *,
        min_buy_pressure_5m: float = 0.35,
        min_acceleration: float = 0.25,
        max_run_up_1h_pct: float = 400.0,
        min_multiplier: float = 0.6,
        max_multiplier: float = 1.25,
        enabled: bool = True,
    ) -> None:
        self.min_buy_pressure_5m = min_buy_pressure_5m
        self.min_acceleration = min_acceleration
        self.max_run_up_1h_pct = max_run_up_1h_pct
        self.min_multiplier = min_multiplier
        self.max_multiplier = max_multiplier
        self.enabled = enabled

    @staticmethod
    def _pressure(buys: int, sells: int) -> float | None:
        total = buys + sells
        # Too few transactions to read anything into the split.
        if total < 5:
            return None
        return buys / total

    def analyze(self, market: MarketData | None) -> FlowAnalysis:
        if market is None:
            return FlowAnalysis(mint="?", notes=["no market data"])

        f = FlowAnalysis(mint=market.mint)
        if not self.enabled:
            return f

        f.buy_pressure_5m = self._pressure(market.buys_5m, market.sells_5m)
        f.buy_pressure_1h = self._pressure(market.buys_1h, market.sells_1h)
        f.run_up_1h = market.price_change_1h

        # Acceleration: extrapolate the last 5 minutes to an hourly rate and
        # compare against the actual trailing hour. 1.0 means the current pace
        # matches the hour's average.
        if market.volume_1h_usd and market.volume_5m_usd is not None:
            if market.volume_1h_usd > 0:
                f.acceleration = (market.volume_5m_usd * 12.0) / market.volume_1h_usd

        self._apply_vetoes(f)
        if f.blocked:
            f.multiplier = 0.0
            return f

        self._score(f)
        return f

    def _apply_vetoes(self, f: FlowAnalysis) -> None:
        """Hard blocks. Each is a case where the trader signal is real but
        acting on it now would be a mistake."""
        if f.buy_pressure_5m is not None and f.buy_pressure_5m < self.min_buy_pressure_5m:
            f.veto = (
                f"distribution_now(only {f.buy_pressure_5m:.0%} of the last "
                f"5min of trades were buys)"
            )
            return

        if f.acceleration is not None and f.acceleration < self.min_acceleration:
            f.veto = (
                f"move_is_over(current volume is {f.acceleration:.0%} of the "
                f"trailing hourly rate)"
            )
            return

        # Already-run tokens: the trader signal may be genuine, but they got
        # in far earlier than you can. Entering here is buying their exit.
        if (
            f.run_up_1h is not None
            and f.run_up_1h > self.max_run_up_1h_pct
            and (f.acceleration is None or f.acceleration < 1.0)
        ):
            f.veto = (
                f"already_ran(+{f.run_up_1h:.0f}% in the last hour and "
                f"decelerating)"
            )

    def _score(self, f: FlowAnalysis) -> None:
        components: list[float] = []

        if f.buy_pressure_5m is not None:
            # 0.5 (balanced) maps to neutral; 0.8+ is strong accumulation.
            components.append(min(1.0, max(0.0, (f.buy_pressure_5m - 0.35) / 0.4)))
            if f.buy_pressure_5m >= 0.70:
                f.notes.append(f"strong buy pressure ({f.buy_pressure_5m:.0%} buys, 5m)")
            elif f.buy_pressure_5m < 0.45:
                f.notes.append(f"weak buy pressure ({f.buy_pressure_5m:.0%} buys, 5m)")

        if f.acceleration is not None:
            components.append(min(1.0, f.acceleration / 2.0))
            if f.acceleration >= 1.5:
                f.notes.append(f"volume accelerating ({f.acceleration:.1f}x hourly pace)")
            elif f.acceleration < 0.6:
                f.notes.append(f"volume fading ({f.acceleration:.1f}x hourly pace)")

        if f.run_up_1h is not None and f.run_up_1h > self.max_run_up_1h_pct / 2:
            f.notes.append(f"already up {f.run_up_1h:.0f}% this hour - late entry risk")
            components.append(0.25)

        f.momentum_score = sum(components) / len(components) if components else 0.5

        # Map a 0-1 momentum score onto the multiplier band. Neutral (0.5)
        # must map to exactly 1.0 so that unknown flow changes nothing.
        if f.momentum_score >= 0.5:
            span = self.max_multiplier - 1.0
            f.multiplier = 1.0 + span * ((f.momentum_score - 0.5) / 0.5)
        else:
            span = 1.0 - self.min_multiplier
            f.multiplier = 1.0 - span * ((0.5 - f.momentum_score) / 0.5)
        f.multiplier = round(f.multiplier, 3)
