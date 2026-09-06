"""DexScreener flow confirmation and boost penalties.

The property these tests exist to protect: flow data can veto or discount a
signal, but it can never create one. There is no actor behind aggregate
volume, so it must not be able to promote a token to STRONG on its own.
"""

from __future__ import annotations

import pytest

from memebot.enrich.boosts import BoostInfo, BoostTracker
from memebot.enrich.dexscreener import DexScreener, MarketData
from memebot.enrich.flow import FlowAnalyzer
from memebot.models import Source, Tier
from memebot.scoring.clustering import ClusterMap
from memebot.scoring.consensus import ConsensusEngine

from .conftest import make_signal


@pytest.fixture
def flow() -> FlowAnalyzer:
    return FlowAnalyzer()


def market(**over) -> MarketData:
    base = dict(
        mint="M", volume_1h_usd=60_000, volume_5m_usd=5_000,
        buys_5m=30, sells_5m=15, buys_1h=300, sells_1h=200,
        price_change_1h=40.0,
    )
    base.update(over)
    return MarketData(**base)


# --- vetoes ---------------------------------------------------------------
def test_heavy_selling_right_now_is_vetoed(flow):
    """Whatever the traders did twenty minutes ago, if the current tape is
    distribution you would be buying into it."""
    f = flow.analyze(market(buys_5m=5, sells_5m=40))
    assert f.blocked and "distribution_now" in f.veto
    assert f.multiplier == 0.0


def test_collapsed_volume_is_vetoed(flow):
    """Signals arrive with lag; this catches a move that is already over."""
    f = flow.analyze(market(volume_5m_usd=200))
    assert f.blocked and "move_is_over" in f.veto


def test_already_ran_and_decelerating_is_vetoed(flow):
    f = flow.analyze(market(price_change_1h=800.0, volume_5m_usd=3_000))
    assert f.blocked and "already_ran" in f.veto


def test_already_ran_but_still_accelerating_is_allowed(flow):
    """A big run-up is only disqualifying if the move is also fading. Vetoing
    every fast mover would filter out the entire category."""
    f = flow.analyze(
        market(price_change_1h=800.0, volume_5m_usd=15_000, buys_5m=50, sells_5m=10)
    )
    assert not f.blocked


# --- multipliers ----------------------------------------------------------
def test_healthy_flow_gives_a_modest_boost(flow):
    f = flow.analyze(market(volume_5m_usd=9_000, buys_5m=40, sells_5m=10))
    assert 1.0 < f.multiplier <= flow.max_multiplier


def test_weak_flow_discounts(flow):
    f = flow.analyze(market(volume_5m_usd=2_000, buys_5m=12, sells_5m=14))
    assert flow.min_multiplier <= f.multiplier < 1.0


def test_multiplier_is_bounded(flow):
    best = flow.analyze(market(volume_5m_usd=500_000, buys_5m=500, sells_5m=1))
    assert best.multiplier <= flow.max_multiplier


def test_unknown_flow_is_exactly_neutral(flow):
    """Missing data must change nothing. A silent 0.9x on every token with
    thin transaction counts would quietly suppress real signals."""
    assert flow.analyze(None).multiplier == 1.0
    sparse = flow.analyze(
        market(buys_5m=1, sells_5m=1, volume_5m_usd=5_000, volume_1h_usd=60_000)
    )
    assert sparse.multiplier == pytest.approx(1.0, abs=0.02)


def test_disabled_analyzer_is_a_noop():
    f = FlowAnalyzer(enabled=False).analyze(market(buys_5m=1, sells_5m=99))
    assert f.multiplier == 1.0 and not f.blocked


# --- the critical property ------------------------------------------------
def test_flow_cannot_promote_a_weak_signal_to_strong(cfg, now):
    """Aggregate volume has no actor behind it. Perfect flow on a token with
    one buyer must stay below the alert threshold."""
    engine = ConsensusEngine(cfg)
    cand = engine.score(
        "M", [make_signal(mint="M", now=now)], ClusterMap({}), now=now
    )
    perfect = FlowAnalyzer().analyze(
        market(volume_5m_usd=500_000, buys_5m=900, sells_5m=2)
    )
    assert cand.independent_actors == 1        # guard against a vacuous test
    assert cand.conviction > 0
    engine.apply_confirmation(cand, perfect.multiplier)
    assert cand.tier is Tier.NONE


def test_flow_can_demote_a_strong_signal(cfg, now):
    engine = ConsensusEngine(cfg)
    sigs = [
        make_signal(mint="M", actor=f"W{i}", size_usd=8000, now=now)
        for i in range(3)
    ]
    sigs += [
        make_signal(mint="M", source=Source.X, actor=f"@x{i}", size_usd=None, now=now)
        for i in range(2)
    ]
    cand = engine.score("M", sigs, ClusterMap({}), now=now)
    assert cand.tier is Tier.STRONG
    engine.apply_confirmation(cand, 0.6, notes=["volume fading"])
    assert cand.tier is not Tier.STRONG
    assert "volume fading" in cand.rationale


# --- boosts ---------------------------------------------------------------
def test_unboosted_token_is_unpenalised():
    assert BoostTracker().penalty("CLEAN") == (1.0, None)


def test_boost_penalty_scales_with_spend():
    bt = BoostTracker()
    bt._boosts = {
        "SMALL": BoostInfo("SMALL", 10, 30),
        "BIG": BoostInfo("BIG", 200, 900),
    }
    small, _ = bt.penalty("SMALL")
    big, note = bt.penalty("BIG")
    assert big < small < 1.0
    assert note and "boost" in note
    assert big >= 1.0 - bt.max_penalty


def test_young_token_boost_is_penalised_harder():
    """A promotion budget that existed before the community did."""
    bt = BoostTracker()
    bt._boosts = {"M": BoostInfo("M", 100, 200)}
    old, _ = bt.penalty("M", age_minutes=2000)
    young, _ = bt.penalty("M", age_minutes=30)
    assert young < old


def test_boost_refresh_parses_provider_payload():
    bt = BoostTracker()
    entries = [
        {"chainId": "solana", "tokenAddress": "A", "amount": 50, "totalAmount": 300},
        {"chainId": "ethereum", "tokenAddress": "B", "amount": 10, "totalAmount": 10},
        {"chainId": "solana", "tokenAddress": "C"},
        "garbage",
    ]
    for e in entries:
        if not isinstance(e, dict) or e.get("chainId") != "solana":
            continue
        mint = e.get("tokenAddress")
        if not mint:
            continue
        bt._boosts[mint] = BoostInfo(
            mint, float(e.get("amount") or 0), float(e.get("totalAmount") or 0)
        )
    assert "A" in bt._boosts and "B" not in bt._boosts
    assert bt._boosts["A"].total_amount == 300


# --- parsing --------------------------------------------------------------
def test_all_time_windows_are_parsed():
    """Flow analysis needs the m5 bucket, which the original parser dropped."""
    dex = DexScreener()
    pair = {
        "chainId": "solana",
        "baseToken": {"symbol": "X", "address": "M"},
        "priceUsd": "0.001",
        "liquidity": {"usd": 50_000},
        "volume": {"h24": 900_000, "h6": 300_000, "h1": 60_000, "m5": 8_000},
        "txns": {
            "m5": {"buys": 40, "sells": 10},
            "h1": {"buys": 300, "sells": 200},
            "h6": {"buys": 900, "sells": 700},
            "h24": {"buys": 3000, "sells": 2500},
        },
        "priceChange": {"m5": 5, "h1": 40, "h6": 120, "h24": 300},
    }
    m = dex._parse("M", pair)
    assert (m.buys_5m, m.sells_5m) == (40, 10)
    assert (m.buys_6h, m.sells_6h) == (900, 700)
    assert m.volume_6h_usd == 300_000
    assert m.price_change_5m == 5


def test_missing_txn_buckets_default_safely():
    dex = DexScreener()
    m = dex._parse("M", {"chainId": "solana", "baseToken": {"address": "M"},
                         "priceUsd": "0.001"})
    assert m.buys_5m == 0 and m.sells_5m == 0
    assert FlowAnalyzer().analyze(m).multiplier == 1.0
