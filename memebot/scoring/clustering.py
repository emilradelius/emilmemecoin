"""Sybil collapse: deciding when several wallets are really one person.

Without this the entire premise of the bot is exploitable. "Five good wallets
just bought this token" is only meaningful if the five wallets are five
independent decisions. One trader splitting across five wallets - or a group
deliberately manufacturing an apparent consensus to attract copy-traders -
produces exactly the same raw signal.

Three kinds of evidence collapse wallets into one actor:

* **Direct funding** - wallet A sent SOL to wallet B. Strongest evidence.
* **Shared funder** - A and B were both funded by the same wallet.
* **Co-occurrence** - A and B keep buying the same tokens within seconds of
  each other. Independent traders converge on the same token sometimes; they
  do not converge within a 5-minute window a dozen times in a row.

The result is a union-find partition. The consensus engine counts *clusters*,
not wallets.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ..models import Signal, Source
from ..store import Store

log = logging.getLogger(__name__)


class UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression.
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic: lexicographically smaller root wins, so cluster
            # ids are stable across restarts.
            lo, hi = sorted((ra, rb))
            self._parent[hi] = lo

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for node in list(self._parent):
            out[self.find(node)].append(node)
        return dict(out)


@dataclass
class ClusterMap:
    """Maps an actor id to its cluster representative."""

    actor_to_cluster: dict[str, str] = field(default_factory=dict)
    built_at: float = field(default_factory=time.time)

    def cluster_of(self, actor_id: str) -> str:
        return self.actor_to_cluster.get(actor_id, actor_id)

    def collapse(self, actor_ids: list[str]) -> set[str]:
        """Reduce a list of actors to the set of distinct real actors."""
        return {self.cluster_of(a) for a in actor_ids}

    def size(self) -> int:
        return len(set(self.actor_to_cluster.values()))


class ClusterBuilder:
    def __init__(self, cfg, store: Store) -> None:
        c = cfg.section("traders").get("clustering", {})
        self.enabled = c.get("enabled", True)
        self.window = c.get("co_occurrence_window_seconds", 300)
        self.min_events = c.get("co_occurrence_min_events", 4)
        self.min_jaccard = c.get("co_occurrence_min_jaccard", 0.55)
        self.collapse_funding = c.get("collapse_on_direct_funding", True)
        self.collapse_shared_funder = c.get("collapse_on_shared_funder", True)
        self.store = store

    def build(self, signals: list[Signal]) -> ClusterMap:
        uf = UnionFind()
        if not self.enabled:
            return ClusterMap({})

        # --- on-chain funding evidence, recorded by the wallet grapher -----
        for a, b, kind, weight in self.store.wallet_links():
            if kind == "direct_funding" and self.collapse_funding:
                uf.union(a, b)
            elif kind == "shared_funder" and self.collapse_shared_funder:
                uf.union(a, b)
            elif kind == "manual":
                uf.union(a, b)

        # --- behavioural co-occurrence -------------------------------------
        onchain = [s for s in signals if s.source is Source.PUMPFUN]
        by_token: dict[str, list[Signal]] = defaultdict(list)
        for s in onchain:
            by_token[s.token_mint].append(s)

        pair_events: dict[tuple[str, str], int] = defaultdict(int)
        tokens_by_actor: dict[str, set[str]] = defaultdict(set)

        for mint, sigs in by_token.items():
            sigs.sort(key=lambda s: s.ts)
            for s in sigs:
                tokens_by_actor[s.actor_id].add(mint)
            # Count each pair at most once per token, so one popular token
            # cannot by itself link every wallet that touched it.
            seen_pairs: set[tuple[str, str]] = set()
            for i, a in enumerate(sigs):
                for b in sigs[i + 1 :]:
                    if b.ts - a.ts > self.window:
                        break
                    if a.actor_id == b.actor_id:
                        continue
                    key = tuple(sorted((a.actor_id, b.actor_id)))  # type: ignore[assignment]
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)  # type: ignore[arg-type]
                    pair_events[key] += 1  # type: ignore[index]

        for (a, b), events in pair_events.items():
            if events < self.min_events:
                continue
            ta, tb = tokens_by_actor[a], tokens_by_actor[b]
            union_size = len(ta | tb)
            if not union_size:
                continue
            jaccard = len(ta & tb) / union_size
            if jaccard >= self.min_jaccard:
                log.info(
                    "collapsing wallets %s and %s (co-occurred %d times, jaccard %.2f)",
                    a[:8], b[:8], events, jaccard,
                )
                uf.union(a, b)
                self.store.add_wallet_link(a, b, "co_occurrence", jaccard)

        mapping: dict[str, str] = {}
        for root, members in uf.groups().items():
            for m in members:
                mapping[m] = root
        return ClusterMap(mapping)
