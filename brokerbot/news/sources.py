"""News ingestion.

RSS is the primary source: free, universal, no key, and every major financial
publisher offers it. Paid APIs are supported through the same interface but
are not required.

A deliberate design point: sources record ``first_seen_at`` at the moment of
ingestion and never trust the publisher's timestamp for anything the
backtester keys on. See :mod:`brokerbot.news.models`.

Parsing uses the standard library rather than ``feedparser``. RSS 2.0 and Atom
are simple enough that the dependency does not earn its place, and every field
read here is optional-tolerant.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime
from email.utils import parsedate_to_datetime

from .models import NewsItem, utcnow

log = logging.getLogger(__name__)

NS = {"atom": "http://www.w3.org/2005/Atom"}

# Free RSS endpoints. Nordic sources included because a Swedish account will
# mostly trade Stockholm listings, and Swedish-language coverage of those
# names breaks well before the English wires pick it up.
FEEDS: dict[str, str] = {
    "reuters_business": "https://feeds.reuters.com/reuters/businessNews",
    "cnbc_finance": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "seeking_alpha": "https://seekingalpha.com/market_currents.xml",
    "nasdaq_omx": "https://www.nasdaqomxnordic.com/rss/news",
    "placera": "https://www.placera.se/placera/telegram.rss.xml",
    "di_bors": "https://www.di.se/rss/bors",
}


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw).replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


class NewsSource(ABC):
    name: str = "source"

    @abstractmethod
    def fetch(self) -> list[NewsItem]: ...


class RssSource(NewsSource):
    def __init__(self, name: str, url: str, *, timeout: float = 15.0) -> None:
        self.name = name
        self.url = url
        self.timeout = timeout

    def fetch(self) -> list[NewsItem]:
        try:
            req = urllib.request.Request(
                self.url, headers={"User-Agent": "Mozilla/5.0 (brokerbot)"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("feed %s unreachable: %s", self.name, exc)
            return []
        return self.parse(raw)

    def parse(self, raw: bytes) -> list[NewsItem]:
        """Parse RSS 2.0 or Atom. Separated from fetching so it can be tested
        against recorded payloads without a network."""
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            log.warning("feed %s returned unparseable XML: %s", self.name, exc)
            return []

        seen_at = utcnow()
        items: list[NewsItem] = []

        for node in root.iter():
            tag = node.tag.split("}")[-1]
            if tag not in ("item", "entry"):
                continue

            title = _text(node.find("title")) or _text(node.find("atom:title", NS))
            if not title:
                continue

            link = _text(node.find("link")) or _text(node.find("atom:link", NS))
            if not link:
                link_node = node.find("atom:link", NS) or node.find("link")
                if link_node is not None:
                    link = link_node.get("href", "")

            summary = (
                _text(node.find("description"))
                or _text(node.find("atom:summary", NS))
                or _text(node.find("atom:content", NS))
            )
            published = _parse_date(
                _text(node.find("pubDate"))
                or _text(node.find("atom:published", NS))
                or _text(node.find("atom:updated", NS))
                or None
            )

            items.append(NewsItem(
                title=title, source=self.name, url=link,
                summary=summary[:2000], published_at=published,
                first_seen_at=seen_at,
            ))
        return items


class NewsIngester:
    """Polls a set of sources and yields only items not seen before."""

    def __init__(self, sources: list[NewsSource] | None = None) -> None:
        self.sources = sources or [RssSource(n, u) for n, u in FEEDS.items()]
        self._seen_ids: set[str] = set()

    @classmethod
    def from_feeds(cls, names: list[str] | None = None) -> "NewsIngester":
        chosen = {n: FEEDS[n] for n in (names or FEEDS) if n in FEEDS}
        return cls([RssSource(n, u) for n, u in chosen.items()])

    def poll(self) -> list[NewsItem]:
        fresh: list[NewsItem] = []
        for source in self.sources:
            try:
                for item in source.fetch():
                    if item.id in self._seen_ids:
                        continue
                    self._seen_ids.add(item.id)
                    fresh.append(item)
            except Exception:
                log.exception("source %s failed; continuing", source.name)

        if len(self._seen_ids) > 200_000:
            self._seen_ids = set(list(self._seen_ids)[-100_000:])
        log.info("ingested %d new items from %d sources", len(fresh), len(self.sources))
        return fresh
