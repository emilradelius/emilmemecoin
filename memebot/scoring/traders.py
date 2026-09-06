"""Deciding who is worth following.

Two very different problems:

* **Wallets** leave a complete, public record. We can reconstruct their
  positions and compute real PnL, so they are scored on results.
* **X accounts** have no visible PnL, so they are scored on *call accuracy*:
  every time they post a contract address we snapshot the price and grade what
  happened over the next 24 hours.

The X side has an unavoidable cold start. Until roughly two weeks of calls
have been graded, X scores are guesses, which is exactly why
``runtime.shadow_mode`` defaults to on.

Both sides are re-scored on a schedule and demoted automatically when recent
performance decays. Traders go cold; the point of scoring continuously rather
than curating a list by hand is to notice before your money does.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass

from ..config import Config
from ..models import Side, Source, TraderScore
from ..store import Store

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TradeEvent:
    mint: str
    side: Side
    ts: float
    size_usd: float
    price_usd: float


@dataclass(slots=True)
class ClosedPosition:
    mint: str
    cost_usd: float
    proceeds_usd: float
    opened_at: float
    closed_at: float

    @property
    def multiple(self) -> float:
        return self.proceeds_usd / self.cost_usd if self.cost_usd > 0 else 0.0

    @property
    def pnl_usd(self) -> float:
        return self.proceeds_usd - self.cost_usd

    @property
    def hold_seconds(self) -> float:
        return max(0.0, self.closed_at - self.opened_at)

    @property
    def is_win(self) -> bool:
        return self.pnl_usd > 0

    @property
    def is_rug(self) -> bool:
        """Effectively a total loss - the position could not be exited."""
        return self.multiple < 0.05


def reconstruct_positions(trades: list[TradeEvent]) -> list[ClosedPosition]:
    """Collapse a wallet's raw buys and sells into closed positions.

    Averaged-cost rather than FIFO lot tracking: meme coin traders scale in
    and out of one position constantly, and lot-level accounting would split
    a single trade idea into dozens of fragments and distort the hold-time
    and win-rate statistics we actually care about.

    A position opens on the first buy and closes when the holding is fully
    sold. Positions still open at the end of the window are ignored - unclosed
    positions are unrealised, and counting them would let a wallet look good
    purely by refusing to sell its losers.
    """
    by_mint: dict[str, list[TradeEvent]] = defaultdict(list)
    for t in trades:
        by_mint[t.mint].append(t)

    closed: list[ClosedPosition] = []
    for mint, events in by_mint.items():
        events.sort(key=lambda e: e.ts)
        cost = proceeds = tokens = 0.0
        opened_at: float | None = None

        for e in events:
            if e.price_usd <= 0:
                continue
            qty = e.size_usd / e.price_usd
            if e.side is Side.BUY:
                if opened_at is None:
                    opened_at = e.ts
                cost += e.size_usd
                tokens += qty
            else:
                if opened_at is None or tokens <= 0:
                    continue  # a sell with no observed buy; not our position
                qty = min(qty, tokens)
                proceeds += qty * e.price_usd
                tokens -= qty
                if tokens <= 1e-9:
                    closed.append(ClosedPosition(mint, cost, proceeds, opened_at, e.ts))
                    cost = proceeds = tokens = 0.0
                    opened_at = None
    return closed


class WalletScorer:
    def __init__(self, cfg: Config, store: Store) -> None:
        w = cfg.section("traders").get("wallets", {})
        self.lookback_days = w.get("lookback_days", 30)
        self.min_closed = w.get("min_closed_trades", 20)
        self.min_unique = w.get("min_unique_tokens", 12)
        self.min_pnl = w.get("min_realized_pnl_usd", 25000)
        self.min_win_rate = w.get("min_win_rate", 0.35)
        self.min_median_mult = w.get("min_median_multiple", 1.15)
        self.min_hold = w.get("min_avg_hold_seconds", 45)
        self.max_hold = w.get("max_avg_hold_seconds", 172800)
        self.max_rug_rate = w.get("max_rug_rate", 0.25)
        self.demote_decay = w.get("demote_on_7d_decay", 0.40)
        self.store = store

    def score(self, wallet: str, trades: list[TradeEvent]) -> TraderScore:
        ts = TraderScore(actor_id=wallet, source=Source.PUMPFUN)
        closed = reconstruct_positions(trades)

        ts.closed_trades = len(closed)
        ts.unique_tokens = len({c.mint for c in closed})
        if not closed:
            ts.excluded_reason = "no_closed_positions"
            return ts

        ts.realized_pnl_usd = round(sum(c.pnl_usd for c in closed), 2)
        ts.win_rate = round(sum(c.is_win for c in closed) / len(closed), 4)
        ts.median_multiple = round(statistics.median(c.multiple for c in closed), 4)
        ts.avg_hold_seconds = round(
            statistics.mean(c.hold_seconds for c in closed), 1
        )
        ts.rug_rate = round(sum(c.is_rug for c in closed) / len(closed), 4)

        reason = self._disqualify(ts)
        if reason:
            ts.excluded_reason = reason
            ts.tracked = False
            ts.score = 0.0
            return ts

        ts.score = self._composite(ts)
        ts.tracked = ts.score > 0.0
        return ts

    def _disqualify(self, ts: TraderScore) -> str | None:
        """Hard gates. Any one of these means the wallet is not followable."""
        if ts.closed_trades < self.min_closed:
            return f"sample_too_small({ts.closed_trades}<{self.min_closed})"
        if ts.unique_tokens < self.min_unique:
            return f"too_few_tokens({ts.unique_tokens}<{self.min_unique})"
        if ts.realized_pnl_usd < self.min_pnl:
            return f"pnl_too_low(${ts.realized_pnl_usd:,.0f})"
        if ts.win_rate < self.min_win_rate:
            return f"win_rate_low({ts.win_rate:.0%})"
        # The median guards against a record that is one lucky 100x wrapped in
        # losses. The mean would hide that; the median cannot.
        if ts.median_multiple < self.min_median_mult:
            return f"median_multiple_low({ts.median_multiple:.2f})"
        # Sub-minute flippers are snipers and MEV bots. Their edge is latency
        # you do not have - by the time their trade reaches you through a
        # websocket, the move is over. Profitable, but not copyable.
        if ts.avg_hold_seconds < self.min_hold:
            return f"sniper_hold_time({ts.avg_hold_seconds:.0f}s)"
        if ts.avg_hold_seconds > self.max_hold:
            return f"bagholder_hold_time({ts.avg_hold_seconds / 3600:.0f}h)"
        if ts.rug_rate > self.max_rug_rate:
            return f"rug_rate_high({ts.rug_rate:.0%})"
        return None

    def _composite(self, ts: TraderScore) -> float:
        """Blend the metrics into a single 0-1 trust weight.

        Each component is normalised against a "very good" reference value and
        clamped at 1.0, so that a wallet cannot dominate on one dimension
        alone - a huge PnL does not excuse a terrible win rate.
        """
        pnl_n = min(1.0, ts.realized_pnl_usd / 250_000)
        win_n = min(1.0, ts.win_rate / 0.60)
        med_n = min(1.0, max(0.0, (ts.median_multiple - 1.0) / 1.0))
        vol_n = min(1.0, ts.closed_trades / 100)
        div_n = min(1.0, ts.unique_tokens / 40)
        rug_n = max(0.0, 1.0 - ts.rug_rate / self.max_rug_rate)

        score = (
            0.30 * pnl_n
            + 0.22 * win_n
            + 0.20 * med_n
            + 0.10 * vol_n
            + 0.08 * div_n
            + 0.10 * rug_n
        )
        return round(min(1.0, max(0.0, score)), 4)

    def apply_decay_demotion(self, ts: TraderScore) -> TraderScore:
        """Demote a wallet whose recent form has fallen off a cliff."""
        if ts.tracked and ts.score > 0 and ts.score_7d > 0:
            if ts.score_7d < ts.score * (1.0 - self.demote_decay):
                ts.tracked = False
                ts.excluded_reason = (
                    f"7d_decay({ts.score_7d:.2f}_vs_{ts.score:.2f})"
                )
        return ts


class XAccountScorer:
    """Scores X accounts on graded call accuracy."""

    def __init__(self, cfg: Config, store: Store) -> None:
        x = cfg.section("traders").get("x_accounts", {})
        self.lookback_days = x.get("lookback_days", 30)
        self.min_calls = x.get("min_graded_calls", 10)
        self.hit_multiple = x.get("hit_multiple", 2.0)
        self.hit_max_dd = x.get("hit_max_drawdown", 0.50)
        self.min_hit_rate = x.get("min_hit_rate", 0.30)
        self.min_avg_max_mult = x.get("min_avg_max_multiple", 1.8)
        self.max_calls_per_day = x.get("max_calls_per_day", 8)
        self.max_median_at_call = x.get("max_median_multiple_at_call_time", 4.0)
        self.promo_penalty = x.get("promo_penalty", 0.5)
        self.demote_decay = x.get("demote_on_7d_decay", 0.40)
        self.store = store

    def score(self, handle: str, *, now: float | None = None,
              promo_fraction: float = 0.0) -> TraderScore:
        now = now if now is not None else time.time()
        since = now - self.lookback_days * 86400
        graded = self.store.graded_calls_for(handle, since)
        all_calls = self.store.calls_for(handle, since)

        ts = TraderScore(actor_id=handle, source=Source.X)
        ts.graded_calls = len(graded)
        ts.calls_per_day = round(len(all_calls) / max(1, self.lookback_days), 3)

        if len(graded) < self.min_calls:
            ts.excluded_reason = f"too_few_graded_calls({len(graded)}<{self.min_calls})"
            # Neutral-but-untrusted weight during the cold start, so the
            # account can contribute corroboration without driving an alert.
            ts.score = 0.25
            ts.tracked = False
            return ts

        hits = 0
        max_multiples: list[float] = []
        at_call_multiples: list[float] = []

        for row in graded:
            entry = row["price_at_call"] or 0.0
            if entry <= 0:
                continue
            max_mult = (row["max_price_24h"] or 0.0) / entry
            min_mult = (row["min_price_24h"] or 0.0) / entry
            max_multiples.append(max_mult)
            if row["multiple_at_call"]:
                at_call_multiples.append(row["multiple_at_call"])
            # A hit is a call you could actually have captured: it reached the
            # target multiple, and did not first draw down past the stop. This
            # deliberately does not credit a wick that only printed after the
            # position would have been stopped out.
            if max_mult >= self.hit_multiple and min_mult >= (1.0 - self.hit_max_dd):
                hits += 1

        n = len(max_multiples)
        if n == 0:
            ts.excluded_reason = "no_priced_calls"
            return ts

        ts.hit_rate = round(hits / n, 4)
        ts.avg_max_multiple = round(statistics.mean(max_multiples), 4)
        ts.median_multiple_at_call = (
            round(statistics.median(at_call_multiples), 4) if at_call_multiples else 0.0
        )

        reason = self._disqualify(ts)
        if reason:
            ts.excluded_reason = reason
            ts.tracked = False
            ts.score = 0.0
            return ts

        hit_n = min(1.0, ts.hit_rate / 0.55)
        mult_n = min(1.0, max(0.0, (ts.avg_max_multiple - 1.0) / 3.0))
        vol_n = min(1.0, ts.graded_calls / 40)
        # Selectivity: an account making two good calls a week is worth more
        # than one making eight a day with the same hit rate, because the
        # latter is a numbers game you cannot follow with real size.
        sel_n = max(0.0, 1.0 - (ts.calls_per_day / self.max_calls_per_day))

        score = 0.40 * hit_n + 0.30 * mult_n + 0.15 * vol_n + 0.15 * sel_n
        if promo_fraction > 0:
            score *= max(0.0, 1.0 - self.promo_penalty * promo_fraction)

        ts.score = round(min(1.0, max(0.0, score)), 4)
        ts.tracked = ts.score > 0.0
        return ts

    def _disqualify(self, ts: TraderScore) -> str | None:
        if ts.hit_rate < self.min_hit_rate:
            return f"hit_rate_low({ts.hit_rate:.0%})"
        if ts.avg_max_multiple < self.min_avg_max_mult:
            return f"avg_multiple_low({ts.avg_max_multiple:.2f})"
        if ts.calls_per_day > self.max_calls_per_day:
            return f"spray_and_pray({ts.calls_per_day:.1f}/day)"
        # Late callers: if the median token is already up 4x by the time they
        # post, they are not finding things, they are asking for exit
        # liquidity.
        if ts.median_multiple_at_call > self.max_median_at_call:
            return f"late_caller(median_{ts.median_multiple_at_call:.1f}x_at_call)"
        return None
