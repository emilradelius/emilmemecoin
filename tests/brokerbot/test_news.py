"""News ingestion, deduplication, entity resolution, classification, drift."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from brokerbot.backtest.engine import BacktestEngine
from brokerbot.costs import PRESETS
from brokerbot.models import Bar
from brokerbot.news.classify import ClassifierBudget, NewsClassifier, estimate_cost
from brokerbot.news.dedup import Deduplicator, jaccard, shingles
from brokerbot.news.entities import EntityResolver, Instrument
from brokerbot.news.models import (
    Direction, EventType, NewsAssessment, NewsItem, NewsSignal, normalise_title,
)
from brokerbot.news.pipeline import NewsPipeline
from brokerbot.news.sources import RssSource
from brokerbot.strategy.news_drift import NewsDriftStrategy

START = datetime(2026, 1, 1)


def item(title, source="reuters", minutes=0, summary=""):
    return NewsItem(title, source, summary=summary,
                    first_seen_at=START + timedelta(minutes=minutes))


# --- timestamps -----------------------------------------------------------
def test_first_seen_is_independent_of_published():
    """The core look-ahead defence. Publisher timestamps get revised and
    backfilled; ours does not."""
    n = NewsItem("x", "y", published_at=datetime(2026, 1, 1, 10, 0),
                 first_seen_at=datetime(2026, 1, 1, 10, 5))
    assert n.lag_seconds() == 300
    assert n.first_seen_at > n.published_at


def test_lag_is_never_negative():
    """A publisher claiming a future timestamp must not produce negative lag,
    which would read as us having seen it early."""
    n = NewsItem("x", "y", published_at=datetime(2026, 1, 2),
                 first_seen_at=datetime(2026, 1, 1))
    assert n.lag_seconds() == 0


# --- dedup ----------------------------------------------------------------
def test_reprints_collapse_to_one_story():
    items = [
        item("Volvo Q3 profit beats analyst estimates", "reuters"),
        item("Volvo Q3 profit beats analyst estimates", "cnbc", minutes=3),
        item("UPDATE: Volvo Q3 profit beats analyst estimates", "yahoo", minutes=9),
    ]
    groups = Deduplicator().group(items)
    assert len(groups) == 1
    assert groups[0].count == 3
    assert groups[0].distinct_sources == 3


def test_canonical_item_is_the_earliest():
    """Keeping a later reprint would understate our true ingestion lag."""
    items = [
        item("Volvo Q3 profit beats estimates", "cnbc", minutes=10),
        item("Volvo Q3 profit beats estimates", "reuters", minutes=0),
    ]
    assert Deduplicator().group(items)[0].canonical.source == "reuters"


def test_opposite_outcomes_never_merge():
    """The failure that would matter most: merging a beat with a miss."""
    beats = shingles(normalise_title("Volvo Q3 profit beats estimates"))
    misses = shingles(normalise_title("Volvo Q3 profit misses estimates"))
    assert jaccard(beats, misses) < Deduplicator().threshold

    groups = Deduplicator().group([
        item("Volvo Q3 profit beats estimates"),
        item("Volvo Q3 profit misses estimates", minutes=5),
    ])
    assert len(groups) == 2


def test_identical_headline_outside_window_is_separate():
    groups = Deduplicator().group([
        item("Volvo reports Q3 results"),
        item("Volvo reports Q3 results", minutes=60 * 24 * 90),
    ])
    assert len(groups) == 2


def test_unrelated_stories_stay_separate():
    groups = Deduplicator().group([
        item("Volvo Q3 profit beats estimates"),
        item("Ericsson wins 5G contract in India", minutes=2),
    ])
    assert len(groups) == 2


# --- entity resolution ----------------------------------------------------
@pytest.fixture
def resolver() -> EntityResolver:
    return EntityResolver()


def test_explicit_ticker_resolves(resolver):
    m = resolver.primary("$NVDA earnings top forecasts")
    assert m.symbol == "NVDA" and m.method == "explicit_ticker"


def test_company_name_resolves(resolver):
    assert resolver.primary("Volvo Q3 profit beats estimates").symbol == "VOLV-B.ST"


@pytest.mark.parametrize("headline", [
    "Investors shell out for record bond sale",
    "Apple pie recipes for autumn baking",
    "The gap between rich and poor widened",
])
def test_ordinary_word_usage_does_not_match(resolver, headline):
    """Company names that are also ordinary words are the main false-positive
    risk. Acting on the wrong company is worse than missing a story."""
    assert resolver.primary(headline) is None


def test_ambiguous_name_matches_with_financial_context(resolver):
    assert resolver.primary("Apple shares fall after iPhone guidance cut").symbol == "AAPL"


def test_market_roundup_is_dropped(resolver):
    """A headline naming five companies is not a story about any of them."""
    assert resolver.resolve(
        "Stocks to watch: Volvo, Ericsson, Sandvik, SEB and Swedbank"
    ) == []


def test_custom_universe():
    r = EntityResolver([Instrument("FOO.ST", "foobar industries", ["foobar"])])
    assert r.primary("Foobar Industries lifts guidance").symbol == "FOO.ST"
    assert r.primary("Volvo beats estimates") is None


# --- classifier budget ----------------------------------------------------
def test_budget_persists_across_restart(tmp_path):
    """An in-memory-only cap resets on every deploy and crash, which means it
    never actually binds."""
    path = tmp_path / "budget.json"
    b = ClassifierBudget(monthly_usd_cap=1.0, state_path=path)
    for _ in range(200):
        b.record(estimate_cost("claude-opus-5", 700, 200))
    assert not b.can_spend()

    restarted = ClassifierBudget(monthly_usd_cap=1.0, state_path=path)
    assert restarted.spent_usd == pytest.approx(b.spent_usd)
    assert not restarted.can_spend()


def test_budget_without_state_path_still_works():
    b = ClassifierBudget(monthly_usd_cap=1.0)
    assert b.can_spend()
    b.record(2.0)
    assert not b.can_spend()


def test_exhausted_budget_returns_untradeable():
    a = NewsClassifier(monthly_usd_cap=0.0).classify(item("test"))
    assert not a.is_tradeable
    assert "budget" in a.rationale


def test_missing_credentials_degrade_rather_than_crash(monkeypatch):
    """An API outage must stop trading, not start it."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    a = NewsClassifier(monthly_usd_cap=5.0).classify(item("Volvo beats"))
    assert not a.is_tradeable
    assert a.materiality == 0.0


def test_cache_reduces_estimated_cost():
    plain = estimate_cost("claude-opus-5", 700, 200)
    cached = estimate_cost("claude-opus-5", 700, 200, cached_tokens=550)
    assert cached < plain


# --- scoring --------------------------------------------------------------
def test_score_is_multiplicative():
    """A material story that is not novel is old news; confident noise is
    still noise. Any weak dimension must drag the score down."""
    def sig(**kw):
        base = dict(materiality=0.9, novelty=0.9, confidence=0.9)
        base.update(kw)
        return NewsSignal("X", item("t"), NewsAssessment(
            EventType.EARNINGS, Direction.BULLISH, **base))

    assert sig().score > 0.7
    assert sig(novelty=0.1).score < 0.1
    assert sig(confidence=0.1).score < 0.1


def test_noise_is_not_tradeable():
    a = NewsAssessment(EventType.NOISE, Direction.BULLISH, materiality=0.9)
    assert not a.is_tradeable


def test_neutral_direction_is_not_tradeable():
    a = NewsAssessment(EventType.EARNINGS, Direction.NEUTRAL, materiality=0.9)
    assert not a.is_tradeable


# --- RSS parsing ----------------------------------------------------------
def test_rss_and_atom_both_parse():
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Volvo Q3 beats</title><link>https://x.test/1</link>
    <description>Above consensus.</description>
    <pubDate>Tue, 15 Sep 2026 08:00:00 GMT</pubDate></item></channel></rss>"""
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
    <entry><title>SEB raises guidance</title><link href="https://y.test/3"/>
    <summary>Outlook lifted.</summary>
    <published>2026-09-15T09:30:00Z</published></entry></feed>"""
    src = RssSource("t", "u")
    assert len(src.parse(rss)) == 1
    assert len(src.parse(atom)) == 1
    assert src.parse(rss)[0].published_at == datetime(2026, 9, 15, 8, 0)


def test_malformed_feed_returns_empty_not_exception():
    assert RssSource("t", "u").parse(b"<not xml at all") == []


def test_items_without_titles_are_skipped():
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><link>https://x.test/1</link></item></channel></rss>"""
    assert RssSource("t", "u").parse(feed) == []


# --- pipeline -------------------------------------------------------------
class StubClassifier(NewsClassifier):
    def __init__(self, assessment: NewsAssessment) -> None:
        super().__init__(monthly_usd_cap=100.0)
        self.assessment = assessment
        self.calls = 0

    def classify(self, item, *, symbol=None):
        self.calls += 1
        return replace(self.assessment, primary_symbol=symbol)


def test_pipeline_classifies_only_what_survives_cheap_filters():
    """Dedup and entity matching are free; the LLM is not. Running the model
    last is the difference between $1 and $50 for the same output."""
    strong = NewsAssessment(EventType.EARNINGS, Direction.BULLISH,
                            materiality=0.9, novelty=0.9, confidence=0.9)
    clf = StubClassifier(strong)
    pipe = NewsPipeline(classifier=clf, min_score=0.2)

    items = (
        [item("Volvo Q3 profit beats analyst estimates", f"src{i}", minutes=i)
         for i in range(5)]                       # 5 reprints -> 1 story
        + [item("Local bakery wins award", "src9", minutes=9)]   # no entity
    )
    signals = pipe.process(items)
    assert pipe.stats.ingested == 6
    assert pipe.stats.after_dedup == 2
    assert clf.calls == 1, "only the entity-matched, de-duplicated story"
    assert len(signals) == 1
    assert signals[0].duplicate_count == 5


def test_pipeline_watchlist_narrows_spend():
    clf = StubClassifier(NewsAssessment(EventType.EARNINGS, Direction.BULLISH,
                                        materiality=0.9, novelty=0.9, confidence=0.9))
    pipe = NewsPipeline(classifier=clf, watchlist={"ERIC-B.ST"})
    pipe.process([item("Volvo Q3 profit beats estimates")])
    assert clf.calls == 0


def test_pipeline_drops_low_scoring_signals():
    weak = NewsAssessment(EventType.EARNINGS, Direction.BULLISH,
                          materiality=0.3, novelty=0.2, confidence=0.4)
    pipe = NewsPipeline(classifier=StubClassifier(weak), min_score=0.25)
    assert pipe.process([item("Volvo Q3 profit beats estimates")]) == []


# --- drift strategy -------------------------------------------------------
def series(moves: list[float]) -> list[Bar]:
    bars, px = [], 100.0
    for i, m in enumerate(moves):
        o, c = px, px * (1 + m)
        bars.append(Bar("TEST", START + timedelta(days=i), o,
                        max(o, c) * 1.002, min(o, c) * 0.998, c))
        px = c
    return bars


def news_signal(day: int, *, direction=Direction.BULLISH, score=0.9) -> NewsSignal:
    return NewsSignal("TEST", NewsItem("Beat", "reuters",
                                       first_seen_at=START + timedelta(days=day)),
                      NewsAssessment(EventType.EARNINGS, direction,
                                     materiality=score, novelty=score, confidence=score))


@pytest.fixture
def engine() -> BacktestEngine:
    return BacktestEngine(PRESETS["nordic_equities"], starting_cash=100_000)


def test_confirmed_news_is_traded(engine):
    strat = NewsDriftStrategy([news_signal(5)], hold_days=20)
    result = engine.run(strat, series([0.0] * 5 + [0.03] + [0.008] * 25))
    assert result.fills


def test_unconfirmed_news_is_not_traded(engine):
    """A story the market shrugs at was not news."""
    strat = NewsDriftStrategy([news_signal(5)], hold_days=20)
    assert not engine.run(strat, series([0.0] * 40)).fills


def test_market_vetoes_a_wrong_classification(engine):
    """The classifier said bullish and the price fell. No trade. This is what
    makes the strategy robust to the model being wrong."""
    strat = NewsDriftStrategy([news_signal(5)], hold_days=20)
    assert not engine.run(strat, series([0.0] * 5 + [-0.04] + [-0.01] * 30)).fills


def test_bearish_news_does_not_open_a_long(engine):
    strat = NewsDriftStrategy([news_signal(5, direction=Direction.BEARISH)])
    assert not engine.run(strat, series([0.0] * 5 + [0.03] + [0.01] * 25)).fills


def test_stale_signal_expires(engine):
    strat = NewsDriftStrategy([news_signal(0)], signal_ttl_days=3)
    result = engine.run(strat, series([0.0] * 10 + [0.05] + [0.01] * 25))
    assert not result.fills


def test_low_score_signal_ignored(engine):
    strat = NewsDriftStrategy([news_signal(5, score=0.4)], min_score=0.35)
    assert not engine.run(strat, series([0.0] * 5 + [0.03] + [0.008] * 25)).fills


def test_stop_loss_closes_the_position(engine):
    strat = NewsDriftStrategy([news_signal(5)], hold_days=30, stop_loss_pct=0.08)
    result = engine.run(strat, series([0.0] * 5 + [0.03] + [0.01] * 3 + [-0.05] * 5
                                      + [0.0] * 20))
    assert result.trades
    assert result.trades[0].net_pnl < 0


def test_drift_window_closes_the_position(engine):
    strat = NewsDriftStrategy([news_signal(5)], hold_days=10)
    result = engine.run(strat, series([0.0] * 5 + [0.03] + [0.004] * 40))
    assert result.trades
    assert result.trades[0].hold_days >= 10


def test_strategy_never_reads_news_from_the_future():
    """Signals are keyed on first_seen_at and must be invisible before it."""
    strat = NewsDriftStrategy([news_signal(20)])
    early = strat._visible_signals("TEST", START + timedelta(days=5))
    later = strat._visible_signals("TEST", START + timedelta(days=20))
    assert early == []
    assert len(later) == 1
