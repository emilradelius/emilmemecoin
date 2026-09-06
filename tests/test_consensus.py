"""The consensus engine is the core of the product, so these tests pin down
its behaviour rather than just its happy path."""

from __future__ import annotations

import pytest

from memebot.models import Side, Source, Tier
from memebot.scoring.clustering import ClusterMap
from memebot.scoring.consensus import ConsensusEngine

from .conftest import make_signal


@pytest.fixture
def engine(cfg) -> ConsensusEngine:
    return ConsensusEngine(cfg)


def test_single_source_never_reaches_strong(engine, now):
    """Cross-source agreement is the whole premise; one platform alone,
    however many wallets, must not produce a STRONG alert."""
    sigs = [make_signal(actor=f"W{i}", size_usd=50_000, now=now) for i in range(8)]
    cand = engine.score("MINT", sigs, ClusterMap({}), now=now)
    assert cand.tier is not Tier.STRONG
    assert len(cand.distinct_sources) == 1


def test_cross_source_reaches_strong(engine, now):
    sigs = [make_signal(actor=f"W{i}", size_usd=8000, now=now) for i in range(3)]
    sigs += [
        make_signal(source=Source.X, actor=f"@x{i}", size_usd=None, now=now)
        for i in range(2)
    ]
    cand = engine.score("MINT", sigs, ClusterMap({}), now=now)
    assert cand.tier is Tier.STRONG
    assert cand.independent_actors == 5
    assert len(cand.distinct_sources) == 2


def test_sybil_wallets_collapse_to_one_actor(engine, now):
    """Five wallets belonging to one person must not look like five traders.
    This is the attack the whole clustering layer exists to stop."""
    sigs = [make_signal(actor=f"W{i}", size_usd=10_000, now=now) for i in range(5)]
    independent = engine.score("MINT", sigs, ClusterMap({}), now=now)
    sybil = engine.score(
        "MINT", sigs, ClusterMap({f"W{i}": "W0" for i in range(5)}), now=now
    )
    assert independent.independent_actors == 5
    assert sybil.independent_actors == 1
    assert sybil.conviction < independent.conviction / 2


def test_sells_net_against_buys(engine, now):
    buys = [make_signal(actor=f"W{i}", now=now) for i in range(4)]
    mixed = buys + [
        make_signal(actor=f"S{i}", side=Side.SELL, now=now) for i in range(3)
    ]
    assert (
        engine.score("MINT", mixed, ClusterMap({}), now=now).conviction
        < engine.score("MINT", buys, ClusterMap({}), now=now).conviction
    )


def test_same_actor_buying_repeatedly_has_diminishing_returns(engine, now):
    """Otherwise one actor could manufacture consensus by chopping an order."""
    # Compare against a single signal of the same age as the freshest repeat,
    # so the recency factor is held constant and only the repeat bonus varies.
    one = engine.score(
        "MINT", [make_signal(age_seconds=0, now=now)], ClusterMap({}), now=now
    )
    many = engine.score(
        "MINT",
        [make_signal(age_seconds=10 * i, now=now) for i in range(6)],
        ClusterMap({}),
        now=now,
    )
    assert many.independent_actors == 1
    assert many.conviction <= one.conviction * engine.repeat_cap + 1e-6


def test_recency_decay(engine, now):
    fresh = engine.score(
        "MINT", [make_signal(age_seconds=10, now=now)], ClusterMap({}), now=now
    )
    stale = engine.score(
        "MINT", [make_signal(age_seconds=1800, now=now)], ClusterMap({}), now=now
    )
    assert stale.conviction < fresh.conviction


def test_signals_past_ttl_are_dropped(engine, now):
    old = make_signal(age_seconds=engine.ttl_minutes * 60 + 120, now=now)
    cand = engine.score("MINT", [old], ClusterMap({}), now=now)
    assert cand.conviction == 0
    assert cand.signals == []


def test_low_confidence_resolution_is_discounted(engine, now):
    """An ambiguous $TICKER must be worth less than an explicit contract
    address, or ticker collisions become an attack vector."""
    certain = engine.score(
        "MINT",
        [make_signal(source=Source.X, size_usd=None, confidence=1.0, now=now)],
        ClusterMap({}), now=now,
    )
    ambiguous = engine.score(
        "MINT",
        [make_signal(source=Source.X, size_usd=None, confidence=0.3, now=now)],
        ClusterMap({}), now=now,
    )
    assert ambiguous.conviction < certain.conviction


def test_size_factor_is_clamped(engine):
    """One whale must not be able to manufacture a signal alone."""
    assert engine.size_factor(1) == engine.size_min
    assert engine.size_factor(10**9) == engine.size_max
    assert engine.size_factor(engine.size_ref) == pytest.approx(1.0)


def test_risk_off_raises_the_bar(engine, now):
    sigs = [make_signal(actor=f"W{i}", size_usd=8000, now=now) for i in range(3)]
    sigs += [
        make_signal(source=Source.X, actor=f"@x{i}", size_usd=None, now=now)
        for i in range(2)
    ]
    risk_on = engine.score("MINT", sigs, ClusterMap({}), now=now, risk_off=False)
    risk_off = engine.score("MINT", sigs, ClusterMap({}), now=now, risk_off=True)
    assert risk_on.tier is Tier.STRONG
    assert risk_off.tier is not Tier.STRONG


def test_score_all_ranks_by_conviction(engine, now):
    sigs = []
    for mint, count in (("A", 1), ("B", 5), ("C", 3)):
        sigs += [
            make_signal(mint=mint, actor=f"{mint}{i}", now=now) for i in range(count)
        ]
    ranked = engine.score_all(sigs, ClusterMap({}), now=now)
    assert [c.token_mint for c in ranked] == ["B", "C", "A"]
