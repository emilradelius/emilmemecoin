"""Finding wallets worth scoring in the first place.

The scoring in ``traders.py`` answers "is this wallet good?". This module
answers the prior question: "which wallets should I even look at?" - out of
the millions that have touched pump.fun.

The approach is outcome-driven and works on free data:

1. Subscribe to token **graduations** (bonding-curve completions). A
   graduation is the cheapest available proxy for "this token worked" - most
   pump.fun tokens never get there.
2. For each graduated token, look at who bought it *early*, before it was
   obvious.
3. A wallet that keeps showing up early in tokens that later graduate is a
   candidate. Candidates get promoted to full scoring once they have enough
   history.

Two important corrections are applied, because the naive version of this
finds insiders rather than traders:

* Wallets that are early in tokens from **one deployer** repeatedly are not
  predicting anything - they are being told. They are excluded.
* Wallets in the very first block or two are snipers competing on latency,
  not judgement. There is an entry-rank floor.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import Config
from ..models import Side, Source
from ..store import Store

log = logging.getLogger(__name__)


@dataclass
class EarlyBuy:
    wallet: str
    mint: str
    ts: float
    rank: int
    """Position in the token's buy ordering. 0 is the very first buyer."""
    deployer: str | None = None


@dataclass
class WalletCandidate:
    wallet: str
    graduated_hits: int = 0
    total_early_buys: int = 0
    deployers_seen: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    avg_rank: float = 0.0
    first_seen: float = field(default_factory=time.time)

    @property
    def hit_rate(self) -> float:
        return self.graduated_hits / self.total_early_buys if self.total_early_buys else 0.0


class WalletDiscovery:
    def __init__(self, cfg: Config, store: Store) -> None:
        w = cfg.section("traders").get("wallets", {})
        self.exclude_deployer_linked = w.get("exclude_deployer_linked", True)
        self.first_buyer_repeat_threshold = w.get(
            "exclude_first_buyer_repeat_threshold", 3
        )
        self.max_tracked = cfg.get("sources.pumpfun.max_tracked_wallets", 150)
        self.store = store

        # A wallet must be early, but not *first* - ranks 0-1 are latency
        # snipers whose edge you cannot reproduce.
        self.min_rank = 2
        self.max_rank = 40
        # Minimum evidence before a candidate is worth the cost of scoring.
        self.min_graduated_hits = 3
        self.min_hit_rate = 0.10

        self._candidates: dict[str, WalletCandidate] = {}

    def observe_early_buy(self, buy: EarlyBuy) -> None:
        if not (self.min_rank <= buy.rank <= self.max_rank):
            return
        cand = self._candidates.setdefault(buy.wallet, WalletCandidate(buy.wallet))
        prev_total = cand.total_early_buys
        cand.total_early_buys += 1
        cand.avg_rank = (cand.avg_rank * prev_total + buy.rank) / cand.total_early_buys
        if buy.deployer:
            cand.deployers_seen[buy.deployer] += 1

    def observe_graduation(self, mint: str, early_buyers: list[str]) -> None:
        for w in early_buyers:
            cand = self._candidates.get(w)
            if cand:
                cand.graduated_hits += 1

    def promotable(self) -> list[WalletCandidate]:
        """Candidates with enough evidence to be worth full scoring."""
        out: list[WalletCandidate] = []
        for cand in self._candidates.values():
            if cand.graduated_hits < self.min_graduated_hits:
                continue
            if cand.hit_rate < self.min_hit_rate:
                continue
            if self.exclude_deployer_linked and self._is_insider(cand):
                log.info(
                    "excluding %s: repeatedly early on one deployer's tokens",
                    cand.wallet[:8],
                )
                continue
            out.append(cand)
        out.sort(key=lambda c: (c.hit_rate, c.graduated_hits), reverse=True)
        return out[: self.max_tracked]

    def _is_insider(self, cand: WalletCandidate) -> bool:
        """Being early on many tokens from one deployer is not skill.

        A trader who found three different winners found three winners. A
        wallet that is early on three tokens from the same deployer was told,
        and their results are not reproducible by anyone watching from
        outside.
        """
        if not cand.deployers_seen:
            return False
        worst = max(cand.deployers_seen.values())
        return worst >= self.first_buyer_repeat_threshold

    # --- bootstrapping from our own observed history --------------------
    def seed_from_signals(self, lookback_days: int = 30) -> list[str]:
        """Cold-start helper: mine wallets out of already-stored signals.

        Useful on a machine that has been running in shadow mode for a while,
        and as a fallback when no external wallet list is available.
        """
        since = time.time() - lookback_days * 86400
        signals = self.store.signals_since(since)
        by_wallet: dict[str, set[str]] = defaultdict(set)
        for s in signals:
            if s.source is Source.PUMPFUN and s.side is Side.BUY:
                by_wallet[s.actor_id].add(s.token_mint)
        return [w for w, mints in by_wallet.items() if len(mints) >= 5]

    def load_seed_file(self, path: str) -> list[str]:
        """Load a newline-delimited wallet list.

        The fastest cold start is exporting a leaderboard from a tool that has
        already indexed history (GMGN, Cielo, Dune) and dropping the addresses
        in a file. Those wallets are then *re-scored from scratch* by this
        bot's own criteria - the external list is only a shortlist of who to
        look at, never a statement that they are good.
        """
        try:
            with open(path) as fh:
                wallets = [
                    line.strip()
                    for line in fh
                    if line.strip() and not line.startswith("#")
                ]
        except OSError as exc:
            log.warning("could not read wallet seed file %s: %s", path, exc)
            return []
        log.info("loaded %d seed wallets from %s", len(wallets), path)
        return wallets
