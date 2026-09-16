"""TJR / ICT "sweep and shift" intraday model, mechanised.

TJR (Tyler J. Riches) teaches an ICT-derived framework whose daily template is
Power of 3: price *accumulates* in the Asia session, is *manipulated* at the
London open - a sweep that takes the stops resting beyond the Asia range - and
then *distributes* in the real direction during New York. The trade is taken
against the sweep, once structure confirms the reversal.

Nobody trading it discretionarily has to answer "which order block?" before
the fact. A backtest does. Every fuzzy term below is therefore pinned to one
specific reading, chosen before seeing any result, and written down here so
that a disappointing number can be attributed to *this* reading rather than
to the idea. Where the choice was arbitrary it is marked ARBITRARY and made
adjustable, because those are the knobs worth varying before concluding
anything.

The committed definitions
-------------------------
**Asia range** - the high and low of 20:00-24:00 New York time, the evening
before the trading day. ARBITRARY: TJR also uses other consolidations.

**Trading window** - 03:00-11:00 New York, spanning the London open and the
New York AM killzone. Setups are only taken here.

**Sweep** - a bar whose high exceeds the Asia high but whose *close* falls
back below it (a short setup), or whose low undercuts the Asia low while the
close recovers above it (a long setup). The close is what makes it a sweep
rather than a breakout, and it is only knowable at the bar's close - calling
it mid-bar is the beginner error every source warns about.

**Swing pivot** - a bar whose high is the highest of the ``pivot_strength``
bars either side of it. Note that such a pivot is only *confirmed*
``pivot_strength`` bars after it forms, and this code never uses one before
that, which is where look-ahead would otherwise enter.

**Market structure shift (MSS)** - after a sweep, the first bar to *close*
beyond the most recently confirmed opposing pivot.

**Fair value gap (FVG)** - three consecutive bars where the first and third do
not overlap: for a bullish gap, ``low[i] > high[i-2]``. The gap taken is the
most recent one created between the sweep and the MSS - the displacement leg.

**Entry** - a resting limit order at the near edge of that gap, filled if
price retraces into it before the window closes.

**Stop** - the sweep extreme itself.

**Target** - the opposite side of the Asia range: external range liquidity,
the next obvious pool. ARBITRARY: a fixed R multiple is the common
alternative and is available via ``target_r``.

**Size** - ``risk_pct`` of equity divided by the distance from entry to stop,
so every trade risks the same fraction regardless of how wide the stop is.

**One setup per day**, and nothing is held overnight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from ..models import Bar

NY = ZoneInfo("America/New_York")


def to_ny(ts: datetime) -> datetime:
    """Naive-UTC bar timestamp to New York wall time.

    Every session boundary in this model is a New York clock time, and New
    York observes DST on different dates from Stockholm. Comparing raw UTC
    hours would silently move the London open by an hour twice a year.
    """
    return ts.replace(tzinfo=timezone.utc).astimezone(NY)


@dataclass(slots=True)
class Setup:
    """One detected trade, with everything decided before it is entered."""

    day: object
    direction: int                    # +1 long, -1 short
    signal_ts: datetime               # bar whose close confirmed the FVG
    entry: float
    stop: float
    target: float
    asia_high: float
    asia_low: float
    sweep_ts: datetime
    mss_ts: datetime
    notes: list[str] = field(default_factory=list)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_r(self) -> float:
        return abs(self.target - self.entry) / self.risk if self.risk else 0.0


class TjrModel:
    """Scans bars and yields setups. Execution is the bracket engine's job."""

    def __init__(
        self,
        *,
        pivot_strength: int = 2,
        asia_start: int = 20,
        asia_end: int = 24,
        window_start: int = 3,
        window_end: int = 11,
        require_ote: bool = False,
        ote_low: float = 0.62,
        ote_high: float = 0.79,
        target_r: float | None = None,
        min_rr: float = 1.0,
        allow_shorts: bool = True,
    ) -> None:
        self.pivot_strength = pivot_strength
        self.asia_start, self.asia_end = asia_start, asia_end
        self.window_start, self.window_end = window_start, window_end
        self.require_ote = require_ote
        self.ote_low, self.ote_high = ote_low, ote_high
        self.target_r = target_r
        self.min_rr = min_rr
        self.allow_shorts = allow_shorts

    # -------------------------------------------------------------- helpers

    def _pivot_high(self, bars: list[Bar], i: int) -> bool:
        k = self.pivot_strength
        if i - k < 0 or i + k >= len(bars):
            return False
        return all(bars[i].high >= bars[j].high
                   for j in range(i - k, i + k + 1) if j != i)

    def _pivot_low(self, bars: list[Bar], i: int) -> bool:
        k = self.pivot_strength
        if i - k < 0 or i + k >= len(bars):
            return False
        return all(bars[i].low <= bars[j].low
                   for j in range(i - k, i + k + 1) if j != i)

    def _last_confirmed_pivot(self, bars: list[Bar], now: int, high: bool,
                              after: int) -> float | None:
        """Most recent pivot confirmed strictly before bar ``now``.

        A pivot centred on ``j`` is only visible once ``j + k`` has printed,
        so the scan stops at ``now - k``. Dropping that guard is precisely how
        a backtest starts trading on information it did not have.
        """
        k = self.pivot_strength
        for j in range(now - k, max(after, k) - 1, -1):
            if high and self._pivot_high(bars, j):
                return bars[j].high
            if not high and self._pivot_low(bars, j):
                return bars[j].low
        return None

    # ----------------------------------------------------------------- scan

    def find_setups(self, bars: list[Bar]) -> list[Setup]:
        by_day: dict[object, list[int]] = {}
        for i, bar in enumerate(bars):
            by_day.setdefault(to_ny(bar.ts).date(), []).append(i)

        setups: list[Setup] = []
        days = sorted(by_day)

        for d_index in range(1, len(days)):
            day = days[d_index]
            prev = days[d_index - 1]

            # Asia range: 20:00-24:00 the evening before.
            asia = [
                bars[i] for i in by_day[prev]
                if self.asia_start <= to_ny(bars[i].ts).hour < self.asia_end
            ]
            if len(asia) < 6:
                continue
            asia_high = max(b.high for b in asia)
            asia_low = min(b.low for b in asia)
            if asia_high <= asia_low:
                continue

            # No minimum length here. Requiring the day to contain N bars
            # reads bars that have not printed at the moment the setup would
            # be taken - a quiet look-ahead that the truncation test catches.
            # A short or broken day simply fails to complete the
            # sweep -> shift -> gap sequence, which is the honest outcome.
            window = [
                i for i in by_day[day]
                if self.window_start <= to_ny(bars[i].ts).hour < self.window_end
            ]
            if not window:
                continue

            setup = self._scan_day(bars, window, day, asia_high, asia_low)
            if setup is not None:
                setups.append(setup)

        return setups

    def _scan_day(self, bars, window, day, asia_high, asia_low) -> Setup | None:
        sweep_i = sweep_dir = None
        sweep_extreme = 0.0

        for i in window:
            bar = bars[i]

            # --- 1. sweep ---------------------------------------------------
            if sweep_i is None:
                if bar.low < asia_low <= bar.close:
                    sweep_i, sweep_dir, sweep_extreme = i, +1, bar.low
                elif bar.high > asia_high >= bar.close and self.allow_shorts:
                    sweep_i, sweep_dir, sweep_extreme = i, -1, bar.high
                continue

            # The sweep is only a premise. If price simply carries on through
            # the level, the stops were not being hunted - the level broke.
            if sweep_dir == +1 and bar.low < sweep_extreme:
                sweep_extreme = bar.low
            if sweep_dir == -1 and bar.high > sweep_extreme:
                sweep_extreme = bar.high

            # --- 2. market structure shift ----------------------------------
            # The structure being broken is the swing that formed *before*
            # the sweep - the last lower high of the move down into it. Only
            # looking after the sweep finds nothing on the common case, where
            # price reverses straight off the low without pausing to build a
            # new pivot on the way up.
            level = self._last_confirmed_pivot(
                bars, i, high=(sweep_dir == +1), after=window[0]
            )
            if level is None:
                continue
            shifted = (bar.close > level) if sweep_dir == +1 else (bar.close < level)
            if not shifted:
                continue

            # --- 3. the gap left by the displacement ------------------------
            fvg = self._latest_fvg(bars, sweep_i, i, sweep_dir)
            if fvg is None:
                return None
            entry, far_edge = fvg

            stop = sweep_extreme
            if sweep_dir == +1 and entry <= stop:
                return None
            if sweep_dir == -1 and entry >= stop:
                return None

            if self.target_r is not None:
                target = entry + sweep_dir * self.target_r * abs(entry - stop)
            else:
                target = asia_high if sweep_dir == +1 else asia_low

            notes = []
            if self.require_ote:
                # Retracement of the impulse leg, measured from the sweep
                # extreme to the extreme reached by the MSS bar.
                peak = max(bars[j].high for j in range(sweep_i, i + 1)) \
                    if sweep_dir == +1 else \
                    min(bars[j].low for j in range(sweep_i, i + 1))
                leg = abs(peak - sweep_extreme)
                if leg <= 0:
                    return None
                retr = abs(peak - entry) / leg
                if not (self.ote_low <= retr <= self.ote_high):
                    return None
                notes.append(f"OTE {retr:.0%}")

            setup = Setup(
                day=day, direction=sweep_dir, signal_ts=bar.ts,
                entry=entry, stop=stop, target=target,
                asia_high=asia_high, asia_low=asia_low,
                sweep_ts=bars[sweep_i].ts, mss_ts=bar.ts, notes=notes,
            )
            if setup.risk <= 0 or setup.reward_r < self.min_rr:
                return None
            return setup

        return None

    def _latest_fvg(self, bars, start: int, end: int, direction: int):
        """Near and far edge of the most recent gap in the displacement leg."""
        for i in range(end, start + 1, -1):
            if i - 2 < 0:
                break
            a, c = bars[i - 2], bars[i]
            if direction == +1 and c.low > a.high:
                return c.low, a.high          # enter at the top of the gap
            if direction == -1 and c.high < a.low:
                return c.high, a.low
        return None
