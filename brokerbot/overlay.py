"""Paper tracker for the S&P + momentum overlay.

What it models: all of the money stays in the S&P, and the time-series
momentum portfolio is added *on top* at ``overlay`` of equity. Futures need
only a few percent of notional as margin, so the overlay does not have to be
funded by selling shares - which is the whole point, since every version that
sold shares to fund it trailed the S&P in good years by construction.

The borrow cost is charged, because the overlay is leverage even when no cash
moves, and a version that ignores it would quietly overstate the result.

This tracks rather than trades. Nothing here can place an order: it holds
weights, marks them against real closing prices every day, and reports the
result beside the S&P over the identical window. If the two ever disagree
about what day it is, the report says so instead of interpolating.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .data.yahoo import YahooBarSource, YahooError
from .strategy.tsmom import ewma_volatility, month_ends

log = logging.getLogger(__name__)

BENCHMARK = "SPY"

UNIVERSE: dict[str, tuple[str, str]] = {
    "ES=F": ("S&P 500", "equity"),      "NQ=F": ("Nasdaq 100", "equity"),
    "YM=F": ("Dow Jones", "equity"),    "RTY=F": ("Russell 2000", "equity"),
    "ZT=F": ("2-year Treasury", "bond"), "ZF=F": ("5-year Treasury", "bond"),
    "ZN=F": ("10-year Treasury", "bond"), "ZB=F": ("30-year Treasury", "bond"),
    "GC=F": ("Gold", "metal"),          "SI=F": ("Silver", "metal"),
    "HG=F": ("Copper", "metal"),        "PL=F": ("Platinum", "metal"),
    "CL=F": ("Crude oil", "energy"),    "NG=F": ("Natural gas", "energy"),
    "HO=F": ("Heating oil", "energy"),  "RB=F": ("Petrol", "energy"),
    "ZC=F": ("Corn", "ag"),             "ZS=F": ("Soybeans", "ag"),
    "ZW=F": ("Wheat", "ag"),            "KC=F": ("Coffee", "ag"),
    "SB=F": ("Sugar", "ag"),            "CT=F": ("Cotton", "ag"),
    "6E=F": ("Euro", "fx"),             "6J=F": ("Japanese yen", "fx"),
    "6B=F": ("British pound", "fx"),    "6A=F": ("Australian dollar", "fx"),
    "6C=F": ("Canadian dollar", "fx"),  "6S=F": ("Swiss franc", "fx"),
}


@dataclass
class Position:
    symbol: str
    weight: float                 # signed, as a fraction of equity
    trailing_return: float
    volatility: float

    @property
    def side(self) -> str:
        return "long" if self.weight > 0 else "short"


@dataclass
class OverlayState:
    inception: date
    equity: float = 100_000.0
    benchmark_equity: float = 100_000.0
    last_rebalance: str = ""
    positions: dict[str, float] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "inception": self.inception.isoformat(),
            "equity": self.equity,
            "benchmark_equity": self.benchmark_equity,
            "last_rebalance": self.last_rebalance,
            "positions": self.positions,
            "last_prices": self.last_prices,
            "history": self.history,
        }

    @classmethod
    def from_json(cls, d: dict) -> "OverlayState":
        return cls(
            inception=date.fromisoformat(d["inception"]),
            equity=d["equity"], benchmark_equity=d["benchmark_equity"],
            last_rebalance=d.get("last_rebalance", ""),
            positions=d.get("positions", {}),
            last_prices=d.get("last_prices", {}),
            history=d.get("history", []),
        )


class OverlayTracker:
    def __init__(
        self,
        state_dir: Path | str = "data/overlay",
        *,
        overlay: float = 0.30,
        target_vol: float = 0.40,
        max_leverage: float = 3.0,
        lookback_months: int = 12,
        borrow_rate: float = 0.03,
        starting_equity: float = 100_000.0,
    ) -> None:
        self.dir = Path(state_dir)
        self.overlay = overlay
        self.target_vol = target_vol
        self.max_leverage = max_leverage
        self.lookback_months = lookback_months
        self.borrow_rate = borrow_rate
        self.starting_equity = starting_equity
        self.source = YahooBarSource()

    @property
    def path(self) -> Path:
        return self.dir / "state.json"

    def load(self) -> OverlayState | None:
        if not self.path.exists():
            return None
        try:
            return OverlayState.from_json(json.loads(self.path.read_text()))
        except (OSError, ValueError, KeyError) as exc:
            log.error("unreadable state at %s: %s", self.path, exc)
            return None

    def save(self, state: OverlayState) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state.to_json(), indent=2))

    # ------------------------------------------------------------ positions

    def compute_positions(self, history: dict[str, list]) -> list[Position]:
        """Target weights from the most recent completed month.

        Signal and volatility both come from data up to that month end, never
        from what has happened since - the same rule the backtest ran under,
        so live and backtest results stay comparable.
        """
        raw: list[Position] = []
        for symbol, bars in history.items():
            ends = month_ends(bars)
            if len(ends) < self.lookback_months + 1:
                continue
            here = ends[-1]
            past = ends[-(self.lookback_months + 1)]
            trailing = bars[here].close / bars[past].close - 1
            if trailing == 0:
                continue
            rets = [bars[i].close / bars[i - 1].close - 1
                    for i in range(max(1, here - 260), here + 1)]
            vol = ewma_volatility(rets)
            if vol <= 0.02:
                continue
            size = min(self.target_vol / vol, self.max_leverage)
            raw.append(Position(symbol, math.copysign(size, trailing), trailing, vol))

        if not raw:
            return []
        n = len(raw)
        for p in raw:
            p.weight /= n
        raw.sort(key=lambda p: -abs(p.trailing_return))
        return raw

    def fetch(self, symbols: list[str], *, range_: str = "3y") -> dict[str, list]:
        out: dict[str, list] = {}
        for symbol in symbols:
            try:
                bars = self.source.load(symbol, range=range_, interval="1d")
            except YahooError as exc:
                log.warning("%s: %s", symbol, exc)
                continue
            if bars:
                out[symbol] = bars
        return out

    # ---------------------------------------------------------------- cycle

    def cycle(self) -> dict:
        """One day's work: rebalance if the month turned, then mark to market."""
        history = self.fetch(list(UNIVERSE) + [BENCHMARK])
        if BENCHMARK not in history:
            return {"ok": False, "error": f"no {BENCHMARK} data - cannot mark"}

        state = self.load()
        if state is None:
            state = OverlayState(
                inception=date.today(),
                equity=self.starting_equity,
                benchmark_equity=self.starting_equity,
            )

        prices = {s: bars[-1].close for s, bars in history.items()}
        as_of = history[BENCHMARK][-1].ts.date()

        # One record per trading day. The job runs on a schedule but also at
        # load, so a reboot or a manual run can fire twice on the same date;
        # each extra pass sees unchanged prices, books a zero return and
        # charges another day of borrowing, walking equity down for no reason.
        if state.history and state.history[-1]["date"] == as_of.isoformat():
            return {
                "ok": True, "as_of": as_of.isoformat(), "rebalanced": False,
                "equity": state.equity, "benchmark": state.benchmark_equity,
                "positions": len(state.positions), "already_recorded": True,
            }

        # Rebalance on the first cycle of a new month.
        this_month = f"{as_of.year:04d}-{as_of.month:02d}"
        rebalanced = False
        if state.last_rebalance != this_month:
            universe = {s: b for s, b in history.items() if s in UNIVERSE}
            positions = self.compute_positions(universe)
            if positions:
                state.positions = {p.symbol: p.weight for p in positions}
                state.last_rebalance = this_month
                rebalanced = True

        # Mark. The first cycle has no previous close, so it books no return.
        overlay_return = benchmark_return = 0.0
        if state.last_prices:
            for symbol, weight in state.positions.items():
                before, now = state.last_prices.get(symbol), prices.get(symbol)
                if before and now and before > 0:
                    overlay_return += weight * (now / before - 1)
            before, now = state.last_prices.get(BENCHMARK), prices.get(BENCHMARK)
            if before and now and before > 0:
                benchmark_return = now / before - 1

            # Borrowing accrues per calendar day, but a cycle can cover more
            # than one: a shut laptop, a weekend, a holiday. The price return
            # already spans the whole gap, so charging a single day here would
            # hand the strategy free leverage over exactly the stretches when
            # nobody was watching - and flatter it by more than its own edge.
            elapsed = 1
            if state.history:
                previous = date.fromisoformat(state.history[-1]["date"])
                elapsed = max(1, (as_of - previous).days)
            borrow = self.overlay * self.borrow_rate * elapsed / 365
            total = benchmark_return + self.overlay * overlay_return - borrow
            state.equity *= 1 + total
            state.benchmark_equity *= 1 + benchmark_return

        state.last_prices = prices
        state.history.append({
            "date": as_of.isoformat(),
            "equity": round(state.equity, 2),
            "benchmark": round(state.benchmark_equity, 2),
            "overlay_return": round(overlay_return, 6),
            "benchmark_return": round(benchmark_return, 6),
            "positions": len(state.positions),
        })
        self.save(state)

        return {
            "ok": True, "as_of": as_of.isoformat(), "rebalanced": rebalanced,
            "equity": state.equity, "benchmark": state.benchmark_equity,
            "positions": len(state.positions),
        }

    # --------------------------------------------------------------- report

    def render(self) -> str:
        state = self.load()
        if state is None or not state.history:
            return ("Nothing recorded yet. Start it with:\n"
                    "  python -m brokerbot.cli overlay --run")

        first, last = state.history[0], state.history[-1]
        days = len(state.history)
        ours = state.equity / self.starting_equity - 1
        theirs = state.benchmark_equity / self.starting_equity - 1

        lines = [
            "S&P 500 + momentum overlay - paper, no money at risk",
            f"{first['date']} to {last['date']}  ({days} trading days)",
            "",
            f"{'':<22}{'value':>14}{'return':>11}",
            "-" * 47,
            f"{'this strategy':<22}{state.equity:>14,.0f}{ours:>+11.2%}",
            f"{'S&P 500 alone':<22}{state.benchmark_equity:>14,.0f}{theirs:>+11.2%}",
            "-" * 47,
            f"{'difference':<22}{state.equity - state.benchmark_equity:>+14,.0f}"
            f"{ours - theirs:>+11.2%}",
            "",
        ]

        rets = [h["benchmark_return"] + self.overlay * h["overlay_return"]
                for h in state.history[1:]]
        if len(rets) > 2:
            vol = statistics.stdev(rets) * math.sqrt(252)
            peak = worst = 0.0
            eq = self.starting_equity
            for h in state.history:
                eq = h["equity"]
                peak = max(peak, eq)
                worst = max(worst, (peak - eq) / peak if peak else 0)
            lines += [f"volatility {vol:.1%} annualised, worst drop {worst:.1%}", ""]

        lines.append(f"holding {last['positions']} futures positions")

        # The honest health warning, sized to the sample.
        if days < 250:
            lines += [
                "",
                f"{days} days is not a result. The strategy's edge is about 0.5% a",
                "year over the S&P; a fortnight of noise is many times that, so",
                "whichever way this reads right now, it means almost nothing.",
                "It beat the S&P in 4 of the last 9 years - expect it to trail",
                "roughly as often as it leads.",
            ]
        return "\n".join(lines)


def describe_universe() -> str:
    by_class: dict[str, list[str]] = {}
    for symbol, (name, cls) in UNIVERSE.items():
        by_class.setdefault(cls, []).append(f"{name} ({symbol})")
    order = ["equity", "bond", "metal", "energy", "ag", "fx"]
    label = {"equity": "Stock indices", "bond": "Government bonds",
             "metal": "Metals", "energy": "Energy", "ag": "Agriculture",
             "fx": "Currencies"}
    out = []
    for cls in order:
        if cls in by_class:
            out.append(f"{label[cls]} ({len(by_class[cls])})")
            for item in by_class[cls]:
                out.append(f"    {item}")
    return "\n".join(out)
