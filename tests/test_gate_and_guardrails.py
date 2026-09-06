"""Alert volume control, spend control, and trading circuit breakers."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from memebot.alerts.gate import AlertGate
from memebot.execution.guardrails import Guardrails
from memebot.models import Candidate, Source, Tier
from memebot.sources.budget import BudgetGovernor
from memebot.sources.xfeed import estimate_cost


def candidate(mint="M", tier=Tier.STRONG, conviction=6.5) -> Candidate:
    return Candidate(
        token_mint=mint, token_symbol="SYM", conviction=conviction, tier=tier,
        signals=[], independent_actors=3,
        distinct_sources=[Source.PUMPFUN, Source.X],
    )


@pytest.fixture
def gate(cfg, store) -> AlertGate:
    return AlertGate(cfg, store)


@pytest.fixture
def tz():
    return ZoneInfo("Europe/Stockholm")


def test_strong_sends_during_the_day(gate, tz):
    at = datetime(2026, 9, 7, 12, 0, tzinfo=tz).timestamp()
    assert gate.evaluate(candidate(), at=at).send


def test_watch_is_suppressed_by_default(gate, tz):
    at = datetime(2026, 9, 7, 12, 0, tzinfo=tz).timestamp()
    d = gate.evaluate(candidate(tier=Tier.WATCH), at=at)
    assert not d.send and d.reason == "watch_tier_suppressed"


def test_daily_budget_is_enforced(gate, store, tz):
    at = datetime(2026, 9, 7, 12, 0, tzinfo=tz).timestamp()
    for i in range(gate.daily_budget):
        store.record_alert(f"a{i}", "buy", f"M{i}", "strong", 6.0, "x", ts=at)
    d = gate.evaluate(candidate("NEW"), at=at)
    assert not d.send and "daily_budget_exhausted" in d.reason


def test_duplicate_token_is_deduped(gate, store, tz):
    at = datetime(2026, 9, 7, 12, 0, tzinfo=tz).timestamp()
    store.record_alert("a", "buy", "DUP", "strong", 6.0, "x", ts=at - 3600)
    d = gate.evaluate(candidate("DUP"), at=at)
    assert not d.send and "deduped" in d.reason


def test_tier_upgrade_breaks_dedupe(gate, store, tz):
    """watch -> strong is new information and should reach you."""
    at = datetime(2026, 9, 7, 12, 0, tzinfo=tz).timestamp()
    store.record_alert("a", "buy", "DUP", "watch", 3.2, "x", ts=at - 3600)
    assert gate.evaluate(candidate("DUP", tier=Tier.STRONG), at=at).send


def test_quiet_hours_defer_rather_than_drop(gate, tz):
    at = datetime(2026, 9, 7, 3, 0, tzinfo=tz).timestamp()
    d = gate.evaluate(candidate(), at=at)
    assert not d.send and d.defer


def test_deferred_alerts_are_capped_and_expire(gate):
    for i in range(10):
        gate.defer(candidate(f"M{i}", conviction=float(i)))
    assert gate.deferred_count == gate.daily_budget

    stale = candidate("OLD", conviction=99)
    stale.computed_at = time.time() - 20 * 3600
    gate.defer(stale)
    assert all(c.token_mint != "OLD" for c in gate.take_deferred())


# --- budget governor -----------------------------------------------------
def test_budget_stops_before_overspending(store):
    b = BudgetGovernor(store, monthly_usd_cap=1.0, usd_per_1k_tweets=0.15)
    for _ in range(10_000):
        b.record_request(0)
    assert not b.can_spend()
    assert b.throttle_multiplier() == float("inf")


def test_budget_throttles_before_the_cap(store):
    b = BudgetGovernor(store, monthly_usd_cap=20.0, usd_per_1k_tweets=0.15,
                       daily_pacing=False)
    for _ in range(115_000):   # ~$17.25, past the 80% soft stop
        b.record_request(0)
    assert 0.80 <= b.fraction_used < 1.0
    assert 1.0 < b.throttle_multiplier() < float("inf")


def test_batching_is_what_makes_the_budget_work():
    """The design decision this project turns on: batched search queries cost
    roughly 20x less than per-account polling for the same coverage."""
    naive = estimate_cost(40, 120, batch_size=1)
    batched = estimate_cost(40, 120, batch_size=20)
    assert batched["usd_per_month"] <= 20.0
    assert naive["usd_per_month"] > batched["usd_per_month"] * 15


# --- guardrails -----------------------------------------------------------
@pytest.fixture
def rails(cfg, store) -> Guardrails:
    return Guardrails(cfg, store)


def test_fresh_state_allows_trading(rails):
    ok, _ = rails.can_open(open_positions=0, wallet_balance_sol=5.0)
    assert ok


def test_consecutive_losses_halt(rails):
    for _ in range(rails.max_consecutive_losses):
        rails.record_close(-0.2)
    ok, why = rails.can_open(open_positions=0, wallet_balance_sol=5.0)
    assert not ok and "halted" in why


def test_daily_loss_cap_halts(rails):
    rails.record_close(-(rails.max_daily_loss + 0.1))
    ok, _ = rails.can_open(open_positions=0, wallet_balance_sol=5.0)
    assert not ok


def test_halt_requires_explicit_resume(rails):
    rails.halt("test")
    assert not rails.can_open(open_positions=0, wallet_balance_sol=5.0)[0]
    rails.resume()
    assert rails.can_open(open_positions=0, wallet_balance_sol=5.0)[0]


def test_wallet_floor_is_respected(rails):
    ok, why = rails.can_open(open_positions=0, wallet_balance_sol=0.001)
    assert not ok and "below floor" in why


def test_concurrent_position_cap(rails):
    ok, why = rails.can_open(
        open_positions=rails.max_concurrent, wallet_balance_sol=5.0
    )
    assert not ok and "concurrent" in why


def test_position_size_scales_and_clamps(rails):
    base = rails.position_size(5.5, strong_threshold=5.5)
    mid = rails.position_size(8.0, strong_threshold=5.5)
    huge = rails.position_size(500.0, strong_threshold=5.5)
    assert base == rails.base_size
    assert base < mid < rails.max_size
    assert huge == rails.max_size
