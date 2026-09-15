"""News domain types.

One field here matters more than all the others: :attr:`NewsItem.first_seen_at`.

News backtesting is uniquely prone to look-ahead bias, and the cause is
timestamps. A story's ``published_at`` is frequently revised - wires correct
them, aggregators backfill them, and some APIs return the timestamp of the
*latest* revision rather than first publication. Backtest on ``published_at``
and you routinely trade on information hours before anyone could have had it,
which produces spectacular and entirely fictional returns.

So the system records **when our own ingester first saw the item**, separately
and immutably, and the backtester keys on that. It is strictly later than real
publication, which biases results *against* the strategy - the safe direction.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class EventType(str, Enum):
    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    MA = "m_and_a"
    REGULATORY = "regulatory"
    LEGAL = "legal"
    PRODUCT = "product"
    MANAGEMENT = "management"
    ANALYST = "analyst_rating"
    MACRO = "macro"
    OTHER = "other"
    NOISE = "noise"
    """Routine coverage carrying no information - recaps, listicles,
    "3 stocks to watch". The majority of any retail news feed."""


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def normalise_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Used for near-duplicate detection. One wire story appears across dozens of
    aggregators with small title edits; without normalisation each copy looks
    like independent corroboration.
    """
    return _WS.sub(" ", _PUNCT.sub(" ", title.lower())).strip()


@dataclass(slots=True)
class NewsItem:
    title: str
    source: str
    url: str = ""
    summary: str = ""
    published_at: datetime | None = None
    """Publisher's claimed time. Untrusted - may be revised or backfilled."""

    first_seen_at: datetime = field(default_factory=utcnow)
    """When OUR ingester saw it. This is what the backtester keys on."""

    symbols: list[str] = field(default_factory=list)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = hashlib.sha1(
                f"{self.source}|{normalise_title(self.title)}".encode()
            ).hexdigest()[:16]

    @property
    def normalised_title(self) -> str:
        return normalise_title(self.title)

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.summary}".strip()

    def lag_seconds(self) -> float | None:
        """How far behind publication we were. Large values mean the move has
        already happened without us."""
        if self.published_at is None:
            return None
        return max(0.0, (self.first_seen_at - self.published_at).total_seconds())


@dataclass(slots=True)
class NewsAssessment:
    """What the classifier concluded about one story."""

    event_type: EventType = EventType.OTHER
    direction: Direction = Direction.NEUTRAL
    materiality: float = 0.0
    """0-1. Would this plausibly move the share price on its own?"""

    novelty: float = 0.0
    """0-1. New information, or a recap of something already known?"""

    confidence: float = 0.0
    surprise: float = 0.0
    """-1 to 1. For earnings/guidance: beat or miss versus expectation."""

    rationale: str = ""
    primary_symbol: str | None = None
    classified_at: datetime = field(default_factory=utcnow)
    model: str = ""
    cost_usd: float = 0.0

    @property
    def is_tradeable(self) -> bool:
        return (
            self.event_type not in (EventType.NOISE, EventType.OTHER)
            and self.direction is not Direction.NEUTRAL
            and self.materiality > 0
        )


@dataclass(slots=True)
class NewsSignal:
    """A classified, de-duplicated, entity-resolved story ready to act on."""

    symbol: str
    item: NewsItem
    assessment: NewsAssessment
    duplicate_count: int = 1
    """How many copies were collapsed into one. High counts mean broad
    coverage, NOT independent confirmation."""

    @property
    def ts(self) -> datetime:
        return self.item.first_seen_at

    @property
    def score(self) -> float:
        """Combined strength in [0, 1]. Multiplicative, so a low score on any
        dimension disqualifies: a highly material story that is not novel is
        old news, and confidently-classified noise is still noise."""
        a = self.assessment
        return round(a.materiality * a.novelty * a.confidence, 4)
