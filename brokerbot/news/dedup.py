"""Near-duplicate collapsing.

One wire story reaches you through a dozen aggregators, each with a slightly
edited headline. A system that treats those as separate items draws two wrong
conclusions at once: it thinks a single event is broadly corroborated, and it
pays to classify the same story twelve times.

Collapsing is therefore both a correctness fix and the largest cost saving in
the pipeline - it runs *before* the LLM, not after.

The signal that survives is ``duplicate_count``, and it should be read
carefully. Heavy duplication means a story got wide distribution. It does
**not** mean several outlets independently confirmed anything; they almost
always reprinted the same wire copy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from .models import NewsItem

log = logging.getLogger(__name__)

# Words carrying no discriminating power in financial headlines. Left in, they
# inflate similarity between unrelated stories about different companies.
STOPWORDS = frozenset("""
a an the and or but of to in on for with at by from as is are was were be been
its it this that these those has have had will would could should may might
says said say reports report reported update updates new news stock stocks
shares share market markets company companies inc corp ltd ab plc group
""".split())


def shingles(text: str, *, k: int = 2) -> set[str]:
    """Word k-grams with stopwords removed.

    Bigrams rather than single words: "profit beats" and "profit misses" share
    a word but must not look similar, and that distinction is the entire point
    of reading the headline.
    """
    words = [w for w in text.split() if w not in STOPWORDS and len(w) > 1]
    if len(words) < k:
        return set(words)
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


@dataclass
class DuplicateGroup:
    canonical: NewsItem
    """The EARLIEST item in the group. Keeping the earliest matters: it is the
    one whose first_seen_at reflects when the information actually reached
    us, and a later reprint would understate our lag."""

    members: list[NewsItem] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def distinct_sources(self) -> int:
        return len(self.sources)


class Deduplicator:
    def __init__(self, *, similarity_threshold: float = 0.45,
                 window_hours: float = 24.0) -> None:
        """``similarity_threshold`` 0.45 catches reprints of the same wire copy
        while keeping a wide margin against opposite outcomes - "profit beats"
        against "profit misses" scores 0.33 and must never merge.

        Be aware of the limit: this is lexical. A genuine paraphrase
        ("third-quarter profit tops forecasts" vs "Q3 profit beats estimates")
        shares almost no word pairs and will not merge. That costs a duplicate
        classification, which is the cheap direction to fail in - merging two
        genuinely different stories would be the expensive one.
        """
        self.threshold = similarity_threshold
        self.window = timedelta(hours=window_hours)

    def group(self, items: list[NewsItem]) -> list[DuplicateGroup]:
        """Collapse near-duplicates into groups, earliest item canonical."""
        ordered = sorted(items, key=lambda i: i.first_seen_at)
        groups: list[DuplicateGroup] = []
        fingerprints: list[set[str]] = []

        for item in ordered:
            fp = shingles(item.normalised_title)
            matched = False
            for idx, existing in enumerate(groups):
                # Only compare within the time window; an identical headline
                # six months later is a different event.
                if item.first_seen_at - existing.canonical.first_seen_at > self.window:
                    continue
                if jaccard(fp, fingerprints[idx]) >= self.threshold:
                    existing.members.append(item)
                    existing.sources.add(item.source)
                    matched = True
                    break
            if not matched:
                groups.append(DuplicateGroup(
                    canonical=item, members=[item], sources={item.source}
                ))
                fingerprints.append(fp)

        collapsed = len(items) - len(groups)
        if collapsed:
            log.info("collapsed %d duplicates into %d unique stories",
                     collapsed, len(groups))
        return groups
