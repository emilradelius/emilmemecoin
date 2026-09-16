"""Time-series momentum (Moskowitz, Ooi & Pedersen), implemented to spec.

This is the one strategy here with a genuine out-of-sample claim behind it.
The paper documents the effect across 58 liquid futures in four asset classes
over 1965-2009, every single contract positive at a 12-month lookback, and it
replicates across countries in later work. That matters because most published
anomalies do not: Hou, Xue and Zhang could not clear a t-statistic of 1.96 for
65% of the 452 anomalies they retested.

It is also the strategy this repo previously tested *wrongly*. Running
momentum on one stock and comparing it to holding that same stock asks whether
trend-following beats owning Volvo, which is not the claim. The claim is about
a diversified, volatility-scaled, long-and-short portfolio across asset
classes, where the edge comes from many weakly-correlated small bets rather
than from any one of them.

The specification, from the paper
---------------------------------
**Signal** - the sign of the past 12-month excess return. Long if positive,
short if negative. Nothing else: no threshold, no filter, no confirmation.

**Position size** - inversely proportional to the instrument's ex-ante
annualised volatility, so each position contributes comparable risk and the
portfolio is not dominated by whatever happens to be volatile this year.

**Volatility** - exponentially weighted lagged squared daily returns, centre
of mass 60 days, annualised by 261. The paper chose this "due to its
simplicity and lack of look-ahead bias", and applies the estimate from t-1 to
the returns at t. This code does the same.

**Rebalance** - monthly.

Costs are charged on turnover, which matters: the strategy trades every month
and flips sign on whole positions, so a cost model that ignores turnover would
be measuring a portfolio nobody can hold.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date

from ..models import Bar

ANNUALISATION = 261
CENTRE_OF_MASS = 60


def ewma_volatility(returns: list[float], centre_of_mass: int = CENTRE_OF_MASS) -> float:
    """Ex-ante annualised volatility from exponentially weighted daily returns.

    ``delta`` is set so the weights' centre of mass is ``centre_of_mass`` days:
    ``sum(i * (1-delta) * delta**i) = delta / (1 - delta)``.
    """
    if len(returns) < 2:
        return 0.0
    delta = centre_of_mass / (centre_of_mass + 1.0)

    # Most recent return first, so weight i applies to lag i.
    weights = [(1 - delta) * delta ** i for i in range(len(returns))]
    total = sum(weights)
    if total <= 0:
        return 0.0
    weights = [w / total for w in weights]
    recent = list(reversed(returns))

    mean = sum(w * r for w, r in zip(weights, recent))
    var = sum(w * (r - mean) ** 2 for w, r in zip(weights, recent))
    return math.sqrt(max(var, 0.0) * ANNUALISATION)


@dataclass
class TsmomResult:
    months: list[date] = field(default_factory=list)
    returns: list[float] = field(default_factory=list)
    gross_returns: list[float] = field(default_factory=list)
    turnover: list[float] = field(default_factory=list)
    n_positions: list[int] = field(default_factory=list)

    @property
    def equity(self) -> list[float]:
        eq, v = [], 1.0
        for r in self.returns:
            v *= 1 + r
            eq.append(v)
        return eq

    @property
    def total_return(self) -> float:
        return self.equity[-1] - 1.0 if self.returns else 0.0

    @property
    def cagr(self) -> float:
        if not self.returns:
            return 0.0
        years = len(self.returns) / 12.0
        return (1 + self.total_return) ** (1 / years) - 1 if years > 0 else 0.0

    @property
    def volatility(self) -> float:
        if len(self.returns) < 2:
            return 0.0
        return statistics.stdev(self.returns) * math.sqrt(12)

    @property
    def sharpe(self) -> float:
        v = self.volatility
        return (self.cagr / v) if v > 0 else 0.0

    @property
    def max_drawdown(self) -> float:
        peak, worst = 1.0, 0.0
        for v in self.equity:
            peak = max(peak, v)
            worst = max(worst, (peak - v) / peak)
        return worst

    @property
    def cost_drag(self) -> float:
        """Annualised return given up to costs."""
        if not self.returns:
            return 0.0
        g = 1.0
        for r in self.gross_returns:
            g *= 1 + r
        years = len(self.returns) / 12.0
        gross_cagr = g ** (1 / years) - 1
        return gross_cagr - self.cagr


def month_ends(bars: list[Bar]) -> list[int]:
    """Index of the last bar in each calendar month."""
    out = []
    for i, bar in enumerate(bars):
        if i + 1 == len(bars) or (bars[i + 1].ts.year, bars[i + 1].ts.month) != \
                (bar.ts.year, bar.ts.month):
            out.append(i)
    return out


class TimeSeriesMomentum:
    def __init__(
        self,
        *,
        lookback_months: int = 12,
        target_vol: float = 0.40,
        portfolio_vol: float | None = None,
        max_leverage: float = 3.0,
        cost_bps: float = 10.0,
        long_only: bool = False,
    ) -> None:
        self.lookback_months = lookback_months
        self.target_vol = target_vol
        self.portfolio_vol = portfolio_vol
        self.max_leverage = max_leverage
        self.cost_bps = cost_bps
        self.long_only = long_only

    def run(self, series: dict[str, list[Bar]]) -> TsmomResult:
        # Align everything on a shared monthly calendar.
        prepared = {}
        for sym, bars in series.items():
            bars = sorted(bars, key=lambda b: b.ts)
            closes = [b.close for b in bars]
            rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
            prepared[sym] = (bars, closes, rets, month_ends(bars))

        calendar = sorted({
            (bars[i].ts.year, bars[i].ts.month)
            for bars, _, _, ends in prepared.values() for i in ends
        })

        result = TsmomResult()
        weights: dict[str, float] = {}

        for m in range(self.lookback_months, len(calendar) - 1):
            ym = calendar[m]
            new_weights: dict[str, float] = {}

            for sym, (bars, closes, rets, ends) in prepared.items():
                here = _end_index(bars, ends, ym)
                past = _end_index(bars, ends, calendar[m - self.lookback_months])
                if here is None or past is None or here <= past:
                    continue

                # Signal: sign of the past 12-month return, known at `here`.
                trailing = closes[here] / closes[past] - 1
                if trailing == 0:
                    continue
                direction = 1.0 if trailing > 0 else -1.0
                if self.long_only and direction < 0:
                    continue

                # Volatility from daily returns strictly before this month end.
                window = rets[max(0, here - 260):here]
                vol = ewma_volatility(window)
                if vol <= 0.02:
                    continue

                size = min(self.target_vol / vol, self.max_leverage)
                new_weights[sym] = direction * size

            if not new_weights:
                continue

            n = len(new_weights)
            new_weights = {s: w / n for s, w in new_weights.items()}

            # Realise next month's return.
            gross = 0.0
            for sym, w in new_weights.items():
                bars, closes, rets, ends = prepared[sym]
                here = _end_index(bars, ends, ym)
                nxt = _end_index(bars, ends, calendar[m + 1])
                if here is None or nxt is None:
                    continue
                gross += w * (closes[nxt] / closes[here] - 1)

            traded = sum(abs(new_weights.get(s, 0.0) - weights.get(s, 0.0))
                         for s in set(new_weights) | set(weights))
            cost = traded * self.cost_bps / 10_000.0

            result.months.append(date(ym[0], ym[1], 1))
            result.gross_returns.append(gross)
            result.returns.append(gross - cost)
            result.turnover.append(traded)
            result.n_positions.append(n)
            weights = new_weights

        # Optional, and off by default, because it is measured over the whole
        # sample: you cannot know in advance what volatility the strategy will
        # turn out to have. It rescales every month by one constant, so Sharpe
        # and the shape of the equity curve are untouched while CAGR and
        # drawdown are not - which makes it fine for putting two strategies on
        # the same axis and wrong for quoting a return.
        if self.portfolio_vol and result.returns:
            realised = result.volatility
            if realised > 0:
                k = self.portfolio_vol / realised
                result.returns = [r * k for r in result.returns]
                result.gross_returns = [r * k for r in result.gross_returns]
        return result


def _end_index(bars, ends, ym) -> int | None:
    for i in ends:
        if (bars[i].ts.year, bars[i].ts.month) == ym:
            return i
    return None
