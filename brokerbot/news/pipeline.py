"""The news pipeline.

Stage order is the whole design, and it is chosen for cost as much as for
correctness::

    ingest -> deduplicate -> resolve entity -> classify (LLM) -> signal
              ~90% dropped   ~80% dropped      only survivors pay

Deduplication and entity matching are free and remove the overwhelming
majority of a raw feed. Only what survives both reaches the model. Running
the model first - the obvious ordering - would cost roughly fifty times as
much for identical output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .classify import NewsClassifier
from .dedup import Deduplicator
from .entities import EntityResolver
from .models import NewsItem, NewsSignal
from .sources import NewsIngester

log = logging.getLogger(__name__)


@dataclass
class PipelineStats:
    ingested: int = 0
    after_dedup: int = 0
    entity_matched: int = 0
    classified: int = 0
    tradeable: int = 0
    cost_usd: float = 0.0

    def summary(self) -> str:
        def pct(n: int) -> str:
            return f"{n / self.ingested:.0%}" if self.ingested else "-"
        return (
            f"ingested {self.ingested} -> deduped {self.after_dedup} ({pct(self.after_dedup)}) "
            f"-> matched {self.entity_matched} ({pct(self.entity_matched)}) "
            f"-> classified {self.classified} -> tradeable {self.tradeable} "
            f"(${self.cost_usd:.4f})"
        )


class NewsPipeline:
    def __init__(
        self,
        *,
        ingester: NewsIngester | None = None,
        deduplicator: Deduplicator | None = None,
        resolver: EntityResolver | None = None,
        classifier: NewsClassifier | None = None,
        watchlist: set[str] | None = None,
        min_score: float = 0.25,
    ) -> None:
        self.ingester = ingester or NewsIngester()
        self.dedup = deduplicator or Deduplicator()
        self.resolver = resolver or EntityResolver()
        self.classifier = classifier or NewsClassifier()
        self.watchlist = watchlist
        """Only these symbols are classified. Nothing narrows LLM spend as
        effectively as not caring about most of the market."""
        self.min_score = min_score
        self.stats = PipelineStats()

    def process(self, items: list[NewsItem] | None = None) -> list[NewsSignal]:
        raw = items if items is not None else self.ingester.poll()
        stats = PipelineStats(ingested=len(raw))

        groups = self.dedup.group(raw)
        stats.after_dedup = len(groups)

        signals: list[NewsSignal] = []
        for group in groups:
            item = group.canonical
            match = self.resolver.primary(item.text)
            if match is None:
                continue
            if self.watchlist and match.symbol not in self.watchlist:
                continue
            stats.entity_matched += 1

            assessment = self.classifier.classify(item, symbol=match.symbol)
            stats.classified += 1
            stats.cost_usd += assessment.cost_usd

            item.symbols = [match.symbol]
            signal = NewsSignal(
                symbol=match.symbol, item=item, assessment=assessment,
                duplicate_count=group.count,
            )
            # Entity confidence multiplies in: a story we are only 60% sure
            # is about this company should not drive a full-strength trade.
            if not assessment.is_tradeable:
                continue
            if signal.score * match.confidence < self.min_score:
                continue
            stats.tradeable += 1
            signals.append(signal)

        self.stats = stats
        log.info("news pipeline: %s", stats.summary())
        return signals
