"""Deciding whether paper results actually justify trading real money.

This is the gate between "the bot works" and "the bot makes money", and they
are not the same claim. A bot that runs cleanly for a week and is up 30% has
demonstrated almost nothing: meme coins are volatile enough that a coin-flip
strategy produces that outcome regularly, and the only way to tell the
difference is sample size and distribution.

So rather than leaving the go-live decision to how the last few trades felt,
this scores the paper history against fixed criteria and gives a verdict.
The criteria deliberately mirror the ones the bot applies to *other* traders
in ``scoring/traders.py`` - it would be incoherent to reject a wallet for
having one lucky moonshot and twenty losses, then promote yourself to live
trading on exactly that record.

The headline metric is the **median multiple**, not the win rate. A strategy
that wins 30% of the time at 4x beats one that wins 70% at 1.2x, and the
second one feels far better while you are running it.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field

from .config import Config
from .models import Position, Source
from .store import Store

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: str
    critical: bool = True
    """Non-critical checks are warnings; they do not block the verdict."""


@dataclass
class ReadinessReport:
    checks: list[Check] = field(default_factory=list)
    closed_trades: int = 0
    days_running: float = 0.0
    median_multiple: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    total_pnl_sol: float = 0.0
    max_drawdown_pct: float = 0.0
    distinct_tokens: int = 0
    best_trade_share: float = 0.0
    top3_share: float = 0.0
    recommended_live_size_sol: float = 0.0

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.critical and not c.passed]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.critical and not c.passed]

    @property
    def ready(self) -> bool:
        return not self.blocking and self.closed_trades > 0


class ReadinessAssessor:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store

        # Thresholds. These are intentionally strict: the cost of going live
        # too early is losing money, the cost of waiting is a few more weeks
        # of paper trading.
        self.min_trades = 30
        self.min_days = 21
        self.min_distinct_tokens = 20
        # Profit factor, not median multiple. See _profit_factor: meme coins
        # are a positive-skew asset class and gating on the typical trade
        # would reject every strategy that actually works here.
        self.min_profit_factor = 1.30
        self.max_best_trade_share = 0.50
        self.max_top3_share = 0.80
        self.max_drawdown_pct = 35.0

        # Start live at a fraction of the paper position size. Paper results
        # never fully survive contact with real fills, and a smaller size
        # buys you real evidence at a lower price.
        self.live_size_fraction = 0.25

    # --- metrics ----------------------------------------------------------
    @staticmethod
    def _multiple(p: Position) -> float:
        if p.size_sol <= 0:
            return 1.0
        return 1.0 + (p.realized_pnl_sol / p.size_sol)

    @staticmethod
    def _max_drawdown_pct(positions: list[Position], bankroll: float) -> float:
        """Worst peak-to-trough decline of the equity curve, as a percentage.

        Measured against a notional starting bankroll rather than against
        cumulative profit. Measuring against profit alone reports 0% for a
        strategy that loses money for three weeks before recovering, which is
        precisely the run most likely to make someone switch the bot off.

        This is the number that decides whether you would have survived your
        own strategy, so it must not flatter it.
        """
        bankroll = max(bankroll, 1e-9)
        equity = bankroll
        peak = bankroll
        worst = 0.0
        for p in positions:
            equity += p.realized_pnl_sol
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak)
        return round(worst * 100, 2)

    @staticmethod
    def _profit_factor(positions: list[Position]) -> float:
        """Gross profit divided by gross loss.

        The right quality metric for a positive-skew strategy. Meme coin
        trading takes many small losses against a few large wins, so demanding
        that the *typical* trade be profitable would reject every viable
        strategy in this asset class. Profit factor asks the question that
        actually matters: across everything, did the winners outweigh the
        losers, and by enough to survive fees and a bad month?
        """
        gains = sum(p.realized_pnl_sol for p in positions if p.realized_pnl_sol > 0)
        losses = -sum(p.realized_pnl_sol for p in positions if p.realized_pnl_sol < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return round(gains / losses, 3)

    def assess(self, *, mode: str = "paper") -> ReadinessReport:
        positions = self.store.closed_positions(mode=mode)
        r = ReadinessReport()
        r.closed_trades = len(positions)

        if not positions:
            r.checks.append(Check(
                "any_history", False,
                f"No closed {mode} trades yet. Run the bot in {mode} mode "
                f"until it has opened and closed real positions.",
            ))
            return r

        multiples = [self._multiple(p) for p in positions]
        r.median_multiple = round(statistics.median(multiples), 3)
        r.win_rate = round(sum(m > 1.0 for m in multiples) / len(multiples), 3)
        r.total_pnl_sol = round(sum(p.realized_pnl_sol for p in positions), 4)
        r.profit_factor = self._profit_factor(positions)
        bankroll = self.cfg.get("execution.max_position_sol", 1.0) * self.cfg.get(
            "execution.max_concurrent_positions", 5
        )
        r.max_drawdown_pct = self._max_drawdown_pct(positions, bankroll)
        r.distinct_tokens = len({p.token_mint for p in positions})
        r.days_running = round(
            (positions[-1].closed_at - positions[0].opened_at) / 86400.0, 1
        ) if positions[-1].closed_at else 0.0

        profits = [p.realized_pnl_sol for p in positions if p.realized_pnl_sol > 0]
        total_profit = sum(profits)
        if profits and total_profit > 0:
            ranked = sorted(profits, reverse=True)
            r.best_trade_share = round(ranked[0] / total_profit, 3)
            r.top3_share = round(sum(ranked[:3]) / total_profit, 3)

        self._build_checks(r)
        base = self.cfg.get("execution.base_position_sol", 0.25)
        r.recommended_live_size_sol = round(base * self.live_size_fraction, 4)
        return r

    def _build_checks(self, r: ReadinessReport) -> None:
        c = r.checks.append

        c(Check(
            "sample_size", r.closed_trades >= self.min_trades,
            f"{r.closed_trades} closed trades (need {self.min_trades}). "
            f"Below that you are reading noise, not performance.",
        ))

        c(Check(
            "calendar_time", r.days_running >= self.min_days,
            f"{r.days_running:.0f} days of history (need {self.min_days}). "
            f"One good week is a market condition, not an edge.",
        ))

        c(Check(
            "profit_factor", r.profit_factor >= self.min_profit_factor,
            f"Profit factor {r.profit_factor:.2f} "
            f"(need {self.min_profit_factor:.2f}). Winners must outweigh losers "
            f"by enough margin to survive real fills and a bad month. "
            f"(Median trade is {r.median_multiple:.2f}x - expected to be below "
            f"1x in this asset class, which is why it is not the gate.)",
        ))

        c(Check(
            "profitable", r.total_pnl_sol > 0,
            f"Total PnL {r.total_pnl_sol:+.3f} SOL after modelled fees and slippage.",
        ))

        c(Check(
            "token_diversity", r.distinct_tokens >= self.min_distinct_tokens,
            f"{r.distinct_tokens} distinct tokens (need {self.min_distinct_tokens}). "
            f"Results concentrated in a few tokens do not generalise.",
        ))

        # The same guard the bot applies to other traders: a record carried by
        # one lucky trade is not a strategy.
        c(Check(
            "not_one_lucky_trade",
            r.best_trade_share <= self.max_best_trade_share,
            f"Best single trade is {r.best_trade_share:.0%} of all profit "
            f"(limit {self.max_best_trade_share:.0%}). The bot rejects other "
            f"traders for exactly this pattern.",
        ))

        c(Check(
            "not_three_lucky_trades", r.top3_share <= self.max_top3_share,
            f"Top 3 trades are {r.top3_share:.0%} of all profit "
            f"(limit {self.max_top3_share:.0%}). Concentrated profit means the "
            f"result rests on a handful of outcomes you cannot rely on repeating.",
        ))

        c(Check(
            "survivable_drawdown", r.max_drawdown_pct <= self.max_drawdown_pct,
            f"Worst peak-to-trough drawdown {r.max_drawdown_pct:.0f}% "
            f"(limit {self.max_drawdown_pct:.0f}%). Ask honestly whether you "
            f"would have left it running through that.",
            critical=False,
        ))

        halted = (self.store.kv_get("breaker_state") or {}).get("halted", False)
        c(Check(
            "breakers_clear", not halted,
            "Circuit breakers are tripped - clear them with /resume first."
            if halted else "Circuit breakers clear.",
        ))

        # X scores are learned, not configured. Until calls have actually been
        # graded, the X half of every consensus decision was made on a
        # placeholder weight - which means the paper record above was produced
        # by a different bot than the one you would be running live.
        if self.cfg.get("sources.x.enabled", True):
            x_accounts = self.store.tracked_actors(Source.X)
            graded_total = sum(a.graded_calls for a in x_accounts)
            c(Check(
                "x_scores_learned", graded_total > 0,
                f"{graded_total} graded X calls across {len(x_accounts)} tracked "
                f"accounts. Until this is well above zero, X signals are running "
                f"on placeholder weights and the paper record understates how "
                f"much the live bot will differ.",
            ))

    # --- rendering --------------------------------------------------------
    def render(self, r: ReadinessReport, *, html: bool = False) -> str:
        b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
        tick, cross, warn = "PASS", "FAIL", "WARN"

        lines = [b("Live-trading readiness"), ""]
        if r.closed_trades:
            lines += [
                f"{r.closed_trades} closed trades over {r.days_running:.0f} days",
                f"Profit factor {r.profit_factor:.2f}  ·  median {r.median_multiple:.2f}x  ·  "
                f"win rate {r.win_rate:.0%}  ·  PnL {r.total_pnl_sol:+.3f} SOL",
                f"Max drawdown {r.max_drawdown_pct:.0f}%  ·  "
                f"{r.distinct_tokens} tokens",
                "",
            ]
        for check in r.checks:
            mark = tick if check.passed else (cross if check.critical else warn)
            lines.append(f"[{mark}] {check.detail}")

        lines.append("")
        if r.ready:
            lines += [
                b("VERDICT: ready to consider live trading"),
                "",
                f"Start at {r.recommended_live_size_sol} SOL per position "
                f"({self.live_size_fraction:.0%} of your paper size). Paper "
                f"results never fully survive real fills, so buy the evidence "
                f"cheaply first.",
                "",
                "To enable: set execution.live_mode_armed: true in config.yaml "
                "on the machine, put a BURNER wallet key in "
                "TRADING_WALLET_PRIVATE_KEY, then /mode live.",
            ]
        else:
            lines += [
                b("VERDICT: not ready"),
                "",
                f"{len(r.blocking)} blocking issue(s) above. Keep running in "
                f"paper mode - it costs nothing and the evidence is the point.",
            ]
        return "\n".join(lines)
