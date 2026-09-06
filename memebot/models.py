"""Core domain types.

Everything that flows through the pipeline is one of these. The important
design decision here is that a :class:`Signal` is keyed on a *mint address*,
never on a ticker symbol. Ticker collisions in meme coins are not an edge
case - at any moment there are dozens of live tokens called ``$MOON``. The
mint address is the only join key that means anything, so ticker-only
mentions must be resolved (see ``memebot.enrich.resolver``) before they can
participate in consensus.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Source(str, Enum):
    PUMPFUN = "pumpfun"
    X = "x"
    ALPHALEDGER = "alphaledger"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Tier(str, Enum):
    STRONG = "strong"
    WATCH = "watch"
    NONE = "none"


class ExitReason(str, Enum):
    SMART_MONEY_EXIT = "smart_money_exit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    LIQUIDITY_COLLAPSE = "liquidity_collapse"
    SAFETY_REGRESSION = "safety_regression"
    TIME_STOP = "time_stop"
    MANUAL = "manual"


def now_ts() -> float:
    return time.time()


def _uid() -> str:
    return uuid.uuid4().hex[:16]


@dataclass(slots=True)
class Signal:
    """One trader doing one thing with one token, as observed by one source."""

    source: Source
    actor_id: str
    """Wallet address for on-chain sources, handle for social sources."""

    token_mint: str
    """Canonical Solana mint address, or ``cex:SYMBOL`` for non-Solana majors."""

    side: Side
    ts: float = field(default_factory=now_ts)

    token_symbol: str | None = None
    size_usd: float | None = None
    price_usd: float | None = None

    actor_score: float = 0.5
    """Trust weight in [0, 1], filled in by the trader scorer."""

    confidence: float = 1.0
    """How sure we are this signal means what we think it means. Ticker-only
    tweets resolved heuristically land well below 1.0."""

    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=_uid)

    @property
    def is_onchain(self) -> bool:
        return self.source is Source.PUMPFUN

    def age_seconds(self, at: float | None = None) -> float:
        return max(0.0, (at if at is not None else now_ts()) - self.ts)


@dataclass(slots=True)
class TokenSafety:
    """Result of the hard safety gate. ``passed`` is the only thing the
    pipeline branches on; ``failures`` exists so the daily report can tell you
    *why* things were rejected, which is how you learn to tune the thresholds."""

    mint: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    volume_1h_usd: float | None = None
    price_usd: float | None = None
    age_minutes: float | None = None
    holders: int | None = None
    top10_pct: float | None = None
    dev_pct: float | None = None
    rugcheck_score: float | None = None
    checked_at: float = field(default_factory=now_ts)

    def fail(self, reason: str) -> None:
        self.passed = False
        self.failures.append(reason)

    def warn(self, reason: str) -> None:
        self.warnings.append(reason)


@dataclass(slots=True)
class Candidate:
    """A token with accumulated conviction from the consensus engine."""

    token_mint: str
    token_symbol: str | None
    conviction: float
    tier: Tier
    signals: list[Signal]
    independent_actors: int
    distinct_sources: list[Source]
    safety: TokenSafety | None = None
    computed_at: float = field(default_factory=now_ts)

    # Populated for the alert card so a human can sanity-check the machine.
    contributing_actors: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)

    @property
    def buy_signals(self) -> list[Signal]:
        return [s for s in self.signals if s.side is Side.BUY]

    @property
    def sell_signals(self) -> list[Signal]:
        return [s for s in self.signals if s.side is Side.SELL]


@dataclass(slots=True)
class Position:
    """An open position, whether paper or live. Created when a BUY alert
    fires, closed by the exit monitor."""

    token_mint: str
    token_symbol: str | None
    entry_price_usd: float
    size_sol: float
    opened_at: float = field(default_factory=now_ts)

    mode: str = "paper"
    entry_tx: str | None = None
    tokens_held: float = 0.0

    peak_price_usd: float = 0.0
    realized_pnl_sol: float = 0.0
    remaining_fraction: float = 1.0
    ladder_rungs_hit: list[float] = field(default_factory=list)

    # The actors whose buying triggered this. Used by the smart-money exit
    # check: when the people who got us in start leaving, we leave.
    trigger_actors: list[str] = field(default_factory=list)
    closed_at: float | None = None
    close_reason: ExitReason | None = None
    id: str = field(default_factory=_uid)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def age_hours(self, at: float | None = None) -> float:
        return ((at if at is not None else now_ts()) - self.opened_at) / 3600.0

    def multiple(self, price_usd: float) -> float:
        if self.entry_price_usd <= 0:
            return 1.0
        return price_usd / self.entry_price_usd


@dataclass(slots=True)
class Alert:
    kind: str                      # "buy" | "exit" | "system"
    token_mint: str
    token_symbol: str | None
    tier: Tier = Tier.NONE
    text: str = ""
    candidate: Candidate | None = None
    position: Position | None = None
    exit_reason: ExitReason | None = None
    urgent: bool = False
    ts: float = field(default_factory=now_ts)
    id: str = field(default_factory=_uid)


@dataclass(slots=True)
class TraderScore:
    """A scorecard for one wallet or one X account."""

    actor_id: str
    source: Source
    score: float = 0.0
    tracked: bool = False

    # On-chain metrics
    realized_pnl_usd: float = 0.0
    win_rate: float = 0.0
    median_multiple: float = 0.0
    closed_trades: int = 0
    unique_tokens: int = 0
    avg_hold_seconds: float = 0.0
    rug_rate: float = 0.0

    # Social metrics
    graded_calls: int = 0
    hit_rate: float = 0.0
    avg_max_multiple: float = 0.0
    median_multiple_at_call: float = 0.0
    calls_per_day: float = 0.0

    score_7d: float = 0.0
    cluster_id: str | None = None
    excluded_reason: str | None = None
    updated_at: float = field(default_factory=now_ts)
