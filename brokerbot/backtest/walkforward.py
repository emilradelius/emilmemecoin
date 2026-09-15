"""Walk-forward validation: catching the strategy that only worked in hindsight.

A backtest tells you how a rule would have done on data you already have. That
is a weaker claim than it feels like, because you chose the rule *after*
seeing the data. Try enough parameter combinations on ten years of prices and
some of them will look superb through luck alone - with 200 combinations, the
best one looks brilliant even if every single rule is worthless.

Walk-forward validation is the standard defence. Split the history into
consecutive windows; on each, pick the best parameters using only the earlier
(in-sample) part, then measure those parameters on the later (out-of-sample)
part they were never allowed to see. Repeat, rolling forward.

The number that matters is **degradation**: how much worse out-of-sample is
than in-sample. A robust strategy degrades a little. An overfitted one falls
apart, and this is the only cheap way to find that out before your money does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable, Sequence

from ..models import Bar
from ..strategy.base import Strategy
from .engine import BacktestEngine
from .metrics import Metrics

log = logging.getLogger(__name__)


@dataclass
class Window:
    index: int
    in_sample: Metrics
    out_sample: Metrics
    best_params: dict[str, Any]

    @property
    def degradation(self) -> float:
        """How much of the in-sample return survived out-of-sample."""
        if self.in_sample.total_return <= 0:
            return 0.0
        return 1.0 - (self.out_sample.total_return / self.in_sample.total_return)


@dataclass
class WalkForwardReport:
    windows: list[Window] = field(default_factory=list)
    strategy_name: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def avg_in_sample_return(self) -> float:
        if not self.windows:
            return 0.0
        return sum(w.in_sample.total_return for w in self.windows) / len(self.windows)

    @property
    def avg_out_sample_return(self) -> float:
        if not self.windows:
            return 0.0
        return sum(w.out_sample.total_return for w in self.windows) / len(self.windows)

    @property
    def consistency(self) -> float:
        """Fraction of out-of-sample windows that made money.

        Arguably more informative than the average: a strategy that wins in
        two windows out of six and happens to average positive is not
        something you can hold through the losing ones.
        """
        if not self.windows:
            return 0.0
        return sum(w.out_sample.total_return > 0 for w in self.windows) / len(self.windows)

    @property
    def degradation(self) -> float:
        if self.avg_in_sample_return <= 0:
            return 0.0
        return 1.0 - (self.avg_out_sample_return / self.avg_in_sample_return)

    @property
    def robust(self) -> bool:
        return (
            self.avg_out_sample_return > 0
            and self.consistency >= 0.5
            and self.degradation < 0.5
        )

    def render(self) -> str:
        lines = [
            f"Walk-forward validation: {self.strategy_name}",
            f"{len(self.windows)} windows",
            "",
            f"{'window':<8} {'in-sample':>11} {'out-sample':>11}  parameters",
            "-" * 62,
        ]
        for w in self.windows:
            params = ", ".join(f"{k}={v}" for k, v in w.best_params.items())
            lines.append(
                f"{w.index:<8} {w.in_sample.total_return:>10.1%} "
                f"{w.out_sample.total_return:>10.1%}  {params}"
            )
        lines += [
            "",
            f"Average in-sample:  {self.avg_in_sample_return:>+7.1%}",
            f"Average out-sample: {self.avg_out_sample_return:>+7.1%}",
            f"Degradation:        {self.degradation:>7.0%}",
            f"Consistency:        {self.consistency:>7.0%} of windows profitable",
            "",
        ]
        if self.robust:
            lines.append(
                "VERDICT: holds up out-of-sample. That is necessary, not "
                "sufficient - it still has to beat buy-and-hold."
            )
        else:
            reasons = []
            if self.avg_out_sample_return <= 0:
                reasons.append("loses money out-of-sample")
            if self.consistency < 0.5:
                reasons.append(f"profitable in only {self.consistency:.0%} of windows")
            if self.degradation >= 0.5:
                reasons.append(f"{self.degradation:.0%} of in-sample returns vanish")
            lines.append(f"VERDICT: NOT robust - {'; '.join(reasons)}.")
            lines.append(
                "This is what an overfitted strategy looks like. The in-sample "
                "numbers were fitted to noise, not to a real effect."
            )
        lines += [f"  - {n}" for n in self.notes]
        return "\n".join(lines)


class WalkForwardValidator:
    def __init__(
        self,
        engine: BacktestEngine,
        *,
        windows: int = 5,
        in_sample_fraction: float = 0.7,
    ) -> None:
        self.engine = engine
        self.windows = windows
        self.in_sample_fraction = in_sample_fraction

    def run(
        self,
        factory: Callable[..., Strategy],
        bars: list[Bar],
        param_grid: dict[str, Sequence[Any]],
    ) -> WalkForwardReport:
        report = WalkForwardReport(strategy_name=factory().name if not param_grid
                                   else factory.__name__)
        if len(bars) < self.windows * 20:
            report.notes.append("not enough history for meaningful windows")
            return report

        keys = list(param_grid)
        combos = [dict(zip(keys, values)) for values in product(*param_grid.values())]
        if not combos:
            combos = [{}]

        chunk = len(bars) // self.windows
        for i in range(self.windows):
            segment = bars[i * chunk : (i + 1) * chunk]
            if len(segment) < 20:
                continue
            split = int(len(segment) * self.in_sample_fraction)
            in_bars, out_bars = segment[:split], segment[split:]
            if len(in_bars) < 10 or len(out_bars) < 5:
                continue

            best_params: dict[str, Any] = {}
            best_metrics: Metrics | None = None
            for combo in combos:
                try:
                    strat = factory(**combo)
                except (ValueError, TypeError):
                    continue  # invalid combination, e.g. fast >= slow
                m = self.engine.run(strat, in_bars).metrics
                if best_metrics is None or m.total_return > best_metrics.total_return:
                    best_metrics, best_params = m, combo

            if best_metrics is None:
                continue
            try:
                out_metrics = self.engine.run(factory(**best_params), out_bars).metrics
            except (ValueError, TypeError):
                continue

            report.windows.append(Window(
                index=i + 1, in_sample=best_metrics,
                out_sample=out_metrics, best_params=best_params,
            ))

        if len(combos) > 20:
            report.notes.append(
                f"{len(combos)} parameter combinations tested per window - the more "
                f"you try, the more the best in-sample result is luck"
            )
        return report
