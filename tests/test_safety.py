"""The safety gate is what stands between you and a rug, so its default
posture - unknown is unsafe - is tested explicitly."""

from __future__ import annotations

import time

import pytest

from memebot.enrich.dexscreener import DexScreener, MarketData
from memebot.enrich.rugcheck import RiskReport, RugCheck
from memebot.models import TokenSafety
from memebot.scoring.safety import SafetyGate


@pytest.fixture
def gate(cfg) -> SafetyGate:
    return SafetyGate(cfg, DexScreener(), RugCheck())


def good_market(**over) -> MarketData:
    base = dict(
        mint="M", symbol="GOOD", price_usd=0.0004, liquidity_usd=80_000,
        volume_24h_usd=250_000, volume_1h_usd=40_000, buys_1h=120, sells_1h=80,
        pair_created_at=time.time() - 3600,
    )
    base.update(over)
    return MarketData(**base)


def good_report(**over) -> RiskReport:
    base = dict(
        mint="M", fetched=True, score=800, mint_authority_revoked=True,
        freeze_authority_revoked=True, lp_locked_pct=100.0, lp_burned=True,
        total_holders=400, top10_pct=18.0, top_holder_pct=6.0,
        creator_pct=2.0, insider_pct=5.0,
    )
    base.update(over)
    return RiskReport(**base)


def run(gate: SafetyGate, market, report) -> TokenSafety:
    result = TokenSafety(mint="M", passed=True)
    gate._apply_market(result, market)
    gate._apply_risk(result, report)
    return result


def test_clean_token_passes(gate):
    assert run(gate, good_market(), good_report()).passed


def test_missing_market_data_fails(gate):
    r = run(gate, None, good_report())
    assert not r.passed and "no_market_data" in r.failures


def test_failed_rugcheck_lookup_fails_closed(gate):
    """Unknown must be treated as unsafe. If we cannot confirm the deployer
    gave up control, we do not buy."""
    r = run(gate, good_market(), RiskReport(mint="M", fetched=False))
    assert not r.passed and "rugcheck_unavailable" in r.failures


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"liquidity_usd": 4000}, "liquidity_too_low"),
        ({"pair_created_at": time.time() - 60}, "too_new"),
        ({"pair_created_at": time.time() - 90 * 3600}, "too_old"),
        ({"volume_24h_usd": 9_000_000}, "wash_trading_suspected"),
        ({"volume_1h_usd": 500}, "insufficient_1h_volume"),
        ({"buys_1h": 4}, "too_few_buyers_1h"),
    ],
)
def test_market_rejections(gate, override, expected):
    r = run(gate, good_market(**override), good_report())
    assert not r.passed
    assert any(expected in f for f in r.failures), r.failures


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"mint_authority_revoked": False}, "mint_authority_active"),
        ({"freeze_authority_revoked": False}, "freeze_authority_active"),
        ({"mint_authority_revoked": None}, "mint_authority_active"),
        ({"lp_locked_pct": 10.0, "lp_burned": False}, "lp_unlocked"),
        ({"lp_locked_pct": None, "lp_burned": None}, "lp_lock_unknown"),
        ({"total_holders": 12}, "too_few_holders"),
        ({"top10_pct": 65.0}, "top10_concentration"),
        ({"top_holder_pct": 30.0}, "whale_holder"),
        ({"creator_pct": 22.0}, "dev_holds"),
        ({"insider_pct": 60.0}, "bundled_launch"),
        ({"danger_flags": ["Mint Authority still enabled"]}, "rugcheck_danger"),
        ({"score": 9000}, "rugcheck_score"),
    ],
)
def test_risk_rejections(gate, override, expected):
    r = run(gate, good_market(), good_report(**override))
    assert not r.passed
    assert any(expected in f for f in r.failures), r.failures


def test_failures_are_recorded_for_tuning(gate):
    """The daily report needs to say *why* things were filtered, or the
    thresholds can never be tuned with any evidence."""
    r = run(gate, good_market(liquidity_usd=100), good_report(total_holders=3))
    assert len(r.failures) >= 2


def test_lp_account_excluded_from_holder_concentration():
    """The bonding curve holds most of the supply by design; counting it
    would reject every legitimate token."""
    rc = RugCheck()
    rep = rc._parse("M", {
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "topHolders": [
            {"pct": 78.0, "isLiquidityPool": True},
            {"pct": 3.0}, {"pct": 2.0},
        ],
        "markets": [{"lp": {"lpLockedPct": 100.0}}],
        "totalHolders": 300,
    })
    assert rep.top10_pct == pytest.approx(5.0)
    assert rep.top_holder_pct == pytest.approx(3.0)
