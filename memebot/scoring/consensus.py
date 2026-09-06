"""The consensus engine.

The premise: a token that several *independent, individually-good* traders buy
across *different platforms* within a short window is a materially better bet
than any one of those traders' buys alone, because the platforms' failure
modes are uncorrelated. A paid shill campaign moves X. A wallet-copying bot
swarm moves on-chain flow. Something that moves both at once is more likely to
be real.

Conviction for a token is::

    conviction = cross_source_multiplier(n_sources) * SUM over (cluster, source) of
                     actor_score * source_weight * size_factor * recency * confidence

with three deliberate properties:

1. **Contributions are grouped by cluster, not by wallet.** Five wallets
   belonging to one person contribute once. See ``clustering.py`` - without
   this the signal is trivially fakeable.
2. **Repeat buys give diminishing returns.** A wallet buying three times in
   the window is more convincing than buying once, but not three times as
   convincing, or one actor could manufacture a signal by chopping an order.
3. **Sells net against buys.** If half the cohort is already leaving, that is
   not consensus.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict

from ..config import Config
from ..models import Candidate, Side, Signal, Source, Tier
from .clustering import ClusterMap

log = logging.getLogger(__name__)


class ConsensusEngine:
    def __init__(self, cfg: Config) -> None:
        c = cfg.section("consensus")
        self.window_minutes = c.get("window_minutes", 30)
        self.ttl_minutes = c.get("signal_ttl_minutes", 45)
        self.half_life = c.get("half_life_minutes", 12)
        self.source_weights = {
            Source(k): float(v) for k, v in (c.get("source_weights") or {}).items()
        }
        self.min_actors = c.get("min_independent_actors", 3)
        self.min_sources = c.get("min_distinct_sources", 2)
        self.cross_mult = {
            int(k): float(v) for k, v in (c.get("cross_source_multiplier") or {}).items()
        }
        sf = c.get("size_factor") or {}
        self.size_ref = sf.get("reference_usd", 5000)
        self.size_min = sf.get("min_factor", 0.4)
        self.size_max = sf.get("max_factor", 2.0)
        self.tiers = c.get("tiers") or {}
        regime = c.get("regime") or {}
        self.regime_enabled = regime.get("enabled", True)
        self.regime_multiplier = regime.get("risk_off_conviction_multiplier", 1.5)

        # Diminishing returns on repeat buys by the same actor in one window.
        self.repeat_bonus = 0.15
        self.repeat_cap = 1.5

    # --- component factors -------------------------------------------------
    def recency_factor(self, sig: Signal, now: float) -> float:
        """Exponential decay. Weight halves every ``half_life`` minutes."""
        age_min = sig.age_seconds(now) / 60.0
        if self.half_life <= 0:
            return 1.0
        return 0.5 ** (age_min / self.half_life)

    def size_factor(self, size_usd: float | None) -> float:
        """Log-scaled position size, clamped.

        A $50k buy says more than a $500 buy, but not 100x more - clamping is
        what stops a single whale from manufacturing a signal alone.
        """
        if not size_usd or size_usd <= 0:
            return 1.0
        factor = 1.0 + math.log10(size_usd / self.size_ref) if self.size_ref > 0 else 1.0
        return max(self.size_min, min(self.size_max, factor))

    def signal_weight(self, sig: Signal, now: float) -> float:
        return (
            max(0.0, min(1.0, sig.actor_score))
            * self.source_weights.get(sig.source, 0.5)
            * self.size_factor(sig.size_usd)
            * self.recency_factor(sig, now)
            * max(0.0, min(1.0, sig.confidence))
        )

    def cross_source_multiplier(self, n_sources: int) -> float:
        if n_sources <= 0:
            return 0.0
        if n_sources in self.cross_mult:
            return self.cross_mult[n_sources]
        return max(self.cross_mult.values(), default=1.0) if self.cross_mult else 1.0

    # --- main scoring ------------------------------------------------------
    def score(
        self,
        mint: str,
        signals: list[Signal],
        clusters: ClusterMap,
        *,
        now: float | None = None,
        risk_off: bool = False,
    ) -> Candidate:
        now = now if now is not None else time.time()
        cutoff = now - self.ttl_minutes * 60
        live = [s for s in signals if s.ts >= cutoff and s.token_mint == mint]

        symbol = next((s.token_symbol for s in live if s.token_symbol), None)

        # Group by (cluster, source) so one person with many wallets, or one
        # account posting repeatedly, counts once per platform.
        groups: dict[tuple[str, Source], list[Signal]] = defaultdict(list)
        for s in live:
            groups[(clusters.cluster_of(s.actor_id), s.source)].append(s)

        per_source_positive: dict[Source, float] = defaultdict(float)
        positive_clusters: set[str] = set()
        contributing: list[str] = []
        total = 0.0

        for (cluster, source), sigs in groups.items():
            buys = [s for s in sigs if s.side is Side.BUY]
            sells = [s for s in sigs if s.side is Side.SELL]

            buy_w = max((self.signal_weight(s, now) for s in buys), default=0.0)
            if len(buys) > 1:
                buy_w *= min(self.repeat_cap, 1.0 + self.repeat_bonus * (len(buys) - 1))

            sell_w = max((self.signal_weight(s, now) for s in sells), default=0.0)
            if len(sells) > 1:
                sell_w *= min(self.repeat_cap, 1.0 + self.repeat_bonus * (len(sells) - 1))

            net = buy_w - sell_w
            total += net
            if net > 0:
                positive_clusters.add(cluster)
                per_source_positive[source] += net
                contributing.append(sigs[0].actor_id)

        base = max(0.0, total)
        sources_present = [s for s, w in per_source_positive.items() if w > 0]
        conviction = base * self.cross_source_multiplier(len(sources_present))

        tier = self._tier(
            conviction, len(positive_clusters), len(sources_present), risk_off=risk_off
        )

        cand = Candidate(
            token_mint=mint,
            token_symbol=symbol,
            conviction=round(conviction, 3),
            tier=tier,
            signals=live,
            independent_actors=len(positive_clusters),
            distinct_sources=sources_present,
            contributing_actors=contributing,
        )
        cand.rationale = self._rationale(cand, per_source_positive, risk_off)
        return cand

    def _tier(
        self, conviction: float, actors: int, sources: int, *, risk_off: bool
    ) -> Tier:
        # Risk-off regime raises the bar rather than silencing the bot: when
        # majors are bleeding, meme coin risk appetite dries up and marginal
        # setups stop working, but genuinely exceptional ones still do.
        mult = self.regime_multiplier if (risk_off and self.regime_enabled) else 1.0

        for tier_name, tier_cls in (("strong", Tier.STRONG), ("watch", Tier.WATCH)):
            spec = self.tiers.get(tier_name) or {}
            if (
                conviction >= spec.get("min_conviction", float("inf")) * mult
                and actors >= spec.get("min_independent_actors", self.min_actors)
                and sources >= spec.get("min_distinct_sources", self.min_sources)
            ):
                return tier_cls
        return Tier.NONE

    @staticmethod
    def _rationale(
        cand: Candidate, per_source: dict[Source, float], risk_off: bool
    ) -> list[str]:
        """Human-readable reasons, so you can audit the machine's decision
        instead of trusting a bare number."""
        out: list[str] = []
        if cand.independent_actors:
            out.append(
                f"{cand.independent_actors} independent actors "
                f"(after de-duplicating linked wallets)"
            )
        for src, weight in sorted(per_source.items(), key=lambda kv: -kv[1]):
            n = len({s.actor_id for s in cand.signals if s.source is src})
            out.append(f"{src.value}: {n} actor(s), weight {weight:.2f}")
        if len(cand.distinct_sources) >= 2:
            out.append(f"cross-platform agreement across {len(cand.distinct_sources)} sources")
        sells = cand.sell_signals
        if sells:
            out.append(f"⚠ {len(sells)} sell signal(s) netted against the buys")
        if risk_off:
            out.append("⚠ risk-off regime: thresholds raised")
        return out

    def score_all(
        self,
        signals: list[Signal],
        clusters: ClusterMap,
        *,
        now: float | None = None,
        risk_off: bool = False,
    ) -> list[Candidate]:
        """Score every mint present in the signal set, best first."""
        now = now if now is not None else time.time()
        by_mint: dict[str, list[Signal]] = defaultdict(list)
        for s in signals:
            by_mint[s.token_mint].append(s)
        cands = [
            self.score(mint, sigs, clusters, now=now, risk_off=risk_off)
            for mint, sigs in by_mint.items()
        ]
        cands.sort(key=lambda c: c.conviction, reverse=True)
        return cands
