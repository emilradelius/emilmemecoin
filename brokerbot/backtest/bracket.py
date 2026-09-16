"""Bracket-order backtester for intraday stop-and-target strategies.

The main engine fills at the next bar's open and knows nothing about stops.
That is the right model for a daily strategy holding for weeks, and the wrong
one for a trade whose entire character is a limit entry, a stop a few points
away and a target several times that. Run through the main engine, a TJR
setup would exit five minutes after its stop was hit, which does not measure
the strategy - it measures the engine.

Three decisions here bias *against* the strategy, deliberately, because each
is a place where a backtest can flatter itself:

1. **When a bar contains both the stop and the target, the stop is taken.**
   Five-minute bars do not record the order things happened in. Assuming the
   good outcome on every ambiguous bar is the single easiest way to
   manufacture an edge that does not exist.
2. **Slippage is charged on entry and exit**, in ticks, including on limit
   entries where in practice you might get filled exactly.
3. **Nothing is held overnight**, so a trade going well at the bell is closed
   at the bell rather than credited with what happened next.

Futures costs are modelled per contract - commission plus ticks - rather than
as a percentage of notional. A percentage model is right for shares and badly
wrong here: one NQ contract carries roughly $480,000 of notional for about
$15 of round-turn friction, so charging it 0.25% would invent a cost about
eighty times the real one and bury any strategy under it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models import Bar
from ..strategy.tjr import Setup, to_ny


@dataclass(slots=True)
class BracketTrade:
    setup: Setup
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    contracts: float
    outcome: str                      # target | stop | timeout
    pnl: float
    costs: float
    risk_cash: float = 0.0
    """What the trade actually had at stake, in money.

    Stored rather than derived: the setup's risk is in index points, and
    turning points into money needs the contract's point value, which lives
    on the engine. Deriving it here without that multiplier reports a stop-out
    as -20R instead of -1R, which looks like a catastrophic strategy rather
    than an arithmetic slip.
    """

    @property
    def r_multiple(self) -> float:
        return self.pnl / self.risk_cash if self.risk_cash else 0.0


@dataclass
class BracketResult:
    trades: list[BracketTrade] = field(default_factory=list)
    equity: list[tuple[datetime, float]] = field(default_factory=list)
    starting_cash: float = 100_000.0
    setups_found: int = 0
    setups_unfilled: int = 0

    @property
    def end_equity(self) -> float:
        return self.equity[-1][1] if self.equity else self.starting_cash

    @property
    def total_return(self) -> float:
        return self.end_equity / self.starting_cash - 1.0

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.trades) if self.trades else 0.0

    @property
    def expectancy_r(self) -> float:
        rs = [t.r_multiple for t in self.trades]
        return sum(rs) / len(rs) if rs else 0.0

    @property
    def total_costs(self) -> float:
        return sum(t.costs for t in self.trades)

    @property
    def max_drawdown(self) -> float:
        peak = self.starting_cash
        worst = 0.0
        for _, eq in self.equity:
            peak = max(peak, eq)
            if peak > 0:
                worst = max(worst, (peak - eq) / peak)
        return worst

    @property
    def profit_factor(self) -> float:
        won = sum(t.pnl for t in self.trades if t.pnl > 0)
        lost = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return won / lost if lost > 0 else float("inf") if won > 0 else 0.0


class BracketEngine:
    def __init__(
        self,
        *,
        starting_cash: float = 100_000.0,
        risk_pct: float = 0.01,
        point_value: float = 20.0,        # NQ: $20 per index point
        tick_size: float = 0.25,
        slippage_ticks: float = 1.0,
        commission_per_side: float = 2.25,
        flat_by_hour: int = 16,           # New York
        whole_contracts: bool = False,
    ) -> None:
        self.starting_cash = starting_cash
        self.risk_pct = risk_pct
        self.point_value = point_value
        self.tick_size = tick_size
        self.slippage_ticks = slippage_ticks
        self.commission_per_side = commission_per_side
        self.flat_by_hour = flat_by_hour
        self.whole_contracts = whole_contracts

    def run(self, bars: list[Bar], setups: list[Setup]) -> BracketResult:
        index = {bar.ts: i for i, bar in enumerate(bars)}
        result = BracketResult(starting_cash=self.starting_cash)
        result.setups_found = len(setups)
        equity = self.starting_cash
        result.equity.append((bars[0].ts, equity))
        slip = self.slippage_ticks * self.tick_size

        for setup in setups:
            start = index.get(setup.signal_ts)
            if start is None:
                continue

            trade = self._simulate(bars, start, setup, equity, slip)
            if trade is None:
                result.setups_unfilled += 1
                continue

            equity += trade.pnl
            result.trades.append(trade)
            result.equity.append((trade.exit_ts, equity))

        result.equity.append((bars[-1].ts, equity))
        return result

    def _simulate(self, bars, start, setup, equity, slip) -> BracketTrade | None:
        d = setup.direction
        contracts = (equity * self.risk_pct) / (setup.risk * self.point_value)
        if self.whole_contracts:
            contracts = float(int(contracts))
        if contracts <= 0:
            return None

        entry_i = None
        entry_price = 0.0

        # --- wait for the limit entry, until the day ends -------------------
        for i in range(start + 1, len(bars)):
            bar = bars[i]
            ny = to_ny(bar.ts)
            if ny.date() != setup.day or ny.hour >= self.flat_by_hour:
                return None
            touched = bar.low <= setup.entry if d > 0 else bar.high >= setup.entry
            if touched:
                # A limit fills at its price or better; a gap through it fills
                # at the open, which is better.
                entry_price = (min(setup.entry, bar.open) if d > 0
                               else max(setup.entry, bar.open))
                entry_price += d * slip
                entry_i = i
                break
        if entry_i is None:
            return None

        # --- manage the bracket ---------------------------------------------
        for i in range(entry_i, len(bars)):
            bar = bars[i]
            ny = to_ny(bar.ts)

            hit_stop = bar.low <= setup.stop if d > 0 else bar.high >= setup.stop
            hit_target = bar.high >= setup.target if d > 0 else bar.low <= setup.target

            # Stop wins every tie. See the module docstring.
            if hit_stop:
                return self._close(setup, bars[entry_i].ts, bar.ts, entry_price,
                                   setup.stop - d * slip, contracts, "stop")
            if hit_target:
                return self._close(setup, bars[entry_i].ts, bar.ts, entry_price,
                                   setup.target - d * slip, contracts, "target")
            if ny.date() != setup.day or ny.hour >= self.flat_by_hour:
                return self._close(setup, bars[entry_i].ts, bar.ts, entry_price,
                                   bar.close - d * slip, contracts, "timeout")

        last = bars[-1]
        return self._close(setup, bars[entry_i].ts, last.ts, entry_price,
                           last.close - d * slip, contracts, "timeout")

    def _close(self, setup, entry_ts, exit_ts, entry_price, exit_price,
               contracts, outcome) -> BracketTrade:
        gross = (exit_price - entry_price) * setup.direction * contracts * self.point_value
        costs = 2 * self.commission_per_side * contracts
        return BracketTrade(
            setup=setup, entry_ts=entry_ts, exit_ts=exit_ts,
            entry_price=entry_price, exit_price=exit_price, contracts=contracts,
            outcome=outcome, pnl=gross - costs, costs=costs,
            risk_cash=setup.risk * contracts * self.point_value,
        )


def buy_and_hold(bars: list[Bar], starting_cash: float = 100_000.0) -> float:
    """Total return from holding the instrument over the same bars."""
    return bars[-1].close / bars[0].close - 1.0
