"""Performance metrics, and the comparison that actually decides things.

Most backtest reports lead with total return, which is close to meaningless on
its own: it says nothing about the risk taken to get there, and nothing about
whether you would have done better doing nothing at all.

So every report here carries a **buy-and-hold benchmark**. If a strategy does
not beat holding the same instrument over the same period, after costs, then
it is an elaborate way to pay commission. That comparison is not optional and
cannot be turned off - it is the whole point of measuring.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from ..models import ClosedTrade, EquityPoint, Fill

TRADING_DAYS = 252


@dataclass
class Metrics:
    start_equity: float = 0.0
    end_equity: float = 0.0
    total_return: float = 0.0
    cagr: float = 0.0
    volatility: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    calmar: float = 0.0

    trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    best_trade_share: float = 0.0
    total_costs: float = 0.0
    cost_drag: float = 0.0
    """Costs as a fraction of starting equity. How much the broker made."""

    days: float = 0.0
    exposure: float = 0.0
    """Fraction of the period actually holding something."""

    @property
    def summary(self) -> str:
        return (
            f"return {self.total_return:+.1%}  CAGR {self.cagr:+.1%}  "
            f"Sharpe {self.sharpe:.2f}  maxDD {self.max_drawdown:.1%}  "
            f"{self.trades} trades"
        )


@dataclass
class ComparisonReport:
    strategy: Metrics
    benchmark: Metrics
    symbol: str = ""
    label: str = "strategy"
    notes: list[str] = field(default_factory=list)

    @property
    def excess_return(self) -> float:
        return self.strategy.total_return - self.benchmark.total_return

    @property
    def beats_benchmark(self) -> bool:
        """Beating buy-and-hold on return alone is not enough - it must also
        not have taken materially more risk to get there."""
        return (
            self.strategy.total_return > self.benchmark.total_return
            and self.strategy.sharpe > self.benchmark.sharpe
        )

    def verdict(self) -> str:
        if self.beats_benchmark:
            return "beats buy-and-hold on both return and risk-adjusted return"
        if self.strategy.total_return > self.benchmark.total_return:
            return (
                "higher return than buy-and-hold, but worse risk-adjusted - "
                "the extra return came from taking more risk, not from skill"
            )
        return "does NOT beat buy-and-hold - holding the instrument was better"


def _returns(equity: list[EquityPoint]) -> list[float]:
    out: list[float] = []
    for prev, cur in zip(equity, equity[1:]):
        if prev.total > 0:
            out.append(cur.total / prev.total - 1.0)
    return out


def max_drawdown(equity: list[EquityPoint]) -> float:
    peak = -math.inf
    worst = 0.0
    for point in equity:
        peak = max(peak, point.total)
        if peak > 0:
            worst = max(worst, (peak - point.total) / peak)
    return worst


def compute(
    equity: list[EquityPoint],
    trades: list[ClosedTrade],
    *,
    fills: list[Fill] | None = None,
    risk_free_rate: float = 0.02,
) -> Metrics:
    """Costs are summed from ``fills`` when available, not from closed trades.

    A position that is opened and never closed still paid commission. Summing
    only closed trades reports zero costs for buy-and-hold, which hides real
    money and makes the benchmark look cheaper than it was.
    """
    m = Metrics()
    if len(equity) < 2:
        return m

    m.start_equity = equity[0].total
    m.end_equity = equity[-1].total
    if m.start_equity > 0:
        m.total_return = m.end_equity / m.start_equity - 1.0

    m.days = (equity[-1].ts - equity[0].ts).total_seconds() / 86400.0
    years = m.days / 365.25
    if years > 0 and m.start_equity > 0 and m.end_equity > 0:
        m.cagr = (m.end_equity / m.start_equity) ** (1 / years) - 1.0

    rets = _returns(equity)
    if len(rets) > 1:
        sd = statistics.pstdev(rets)
        m.volatility = sd * math.sqrt(TRADING_DAYS)
        mean = statistics.fmean(rets)
        excess = mean - risk_free_rate / TRADING_DAYS
        if sd > 0:
            m.sharpe = (excess / sd) * math.sqrt(TRADING_DAYS)
        downside = [r for r in rets if r < 0]
        if len(downside) > 1:
            dsd = statistics.pstdev(downside)
            if dsd > 0:
                m.sortino = (excess / dsd) * math.sqrt(TRADING_DAYS)

    m.max_drawdown = max_drawdown(equity)
    if m.max_drawdown > 0:
        m.calmar = m.cagr / m.max_drawdown

    m.trades = len(trades)
    if trades:
        wins = [t.net_pnl for t in trades if t.is_win]
        losses = [-t.net_pnl for t in trades if not t.is_win]
        m.win_rate = len(wins) / len(trades)
        m.avg_win = statistics.fmean(wins) if wins else 0.0
        m.avg_loss = statistics.fmean(losses) if losses else 0.0
        gross_win, gross_loss = sum(wins), sum(losses)
        m.profit_factor = (
            gross_win / gross_loss if gross_loss > 0
            else (math.inf if gross_win > 0 else 0.0)
        )
        m.expectancy = statistics.fmean(t.net_pnl for t in trades)
        if wins and gross_win > 0:
            m.best_trade_share = max(wins) / gross_win
        m.total_costs = sum(t.costs for t in trades)

    if fills is not None:
        m.total_costs = sum(f.total_costs for f in fills)
    if m.start_equity > 0:
        m.cost_drag = m.total_costs / m.start_equity
    return m


def render(report: ComparisonReport) -> str:
    s, b = report.strategy, report.benchmark
    rows = [
        ("Total return", f"{s.total_return:+.1%}", f"{b.total_return:+.1%}"),
        ("CAGR", f"{s.cagr:+.1%}", f"{b.cagr:+.1%}"),
        ("Sharpe", f"{s.sharpe:.2f}", f"{b.sharpe:.2f}"),
        ("Sortino", f"{s.sortino:.2f}", f"{b.sortino:.2f}"),
        ("Max drawdown", f"{s.max_drawdown:.1%}", f"{b.max_drawdown:.1%}"),
        ("Volatility", f"{s.volatility:.1%}", f"{b.volatility:.1%}"),
        ("Trades", f"{s.trades}", f"{b.trades}"),
        ("Win rate", f"{s.win_rate:.0%}", "-"),
        ("Profit factor", f"{s.profit_factor:.2f}", "-"),
        ("Costs paid", f"{s.total_costs:,.0f}", f"{b.total_costs:,.0f}"),
        ("Cost drag", f"{s.cost_drag:.1%}", f"{b.cost_drag:.1%}"),
    ]
    width = max(len(r[0]) for r in rows)
    lines = [
        f"{report.label} vs buy-and-hold" + (f"  [{report.symbol}]" if report.symbol else ""),
        f"{s.days:.0f} days",
        "",
        f"{'':<{width}}  {'strategy':>12} {'buy & hold':>12}",
        "-" * (width + 28),
    ]
    for name, sv, bv in rows:
        lines.append(f"{name:<{width}}  {sv:>12} {bv:>12}")
    lines += ["", f"VERDICT: {report.verdict()}"]
    if not report.beats_benchmark:
        lines.append(
            "A strategy that loses to holding the instrument is not a strategy. "
            "Before tuning it, check the cost drag line - that is money that "
            "went to the broker regardless of whether you were right."
        )
    lines += [f"  - {n}" for n in report.notes]
    return "\n".join(lines)
