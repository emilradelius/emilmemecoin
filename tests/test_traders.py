"""Trader scoring must reject the archetypes that look good on paper."""

from __future__ import annotations

import time

import pytest

from memebot.models import Side, Source
from memebot.scoring.traders import (
    TradeEvent, WalletScorer, XAccountScorer, reconstruct_positions,
)


@pytest.fixture
def scorer(cfg, store) -> WalletScorer:
    return WalletScorer(cfg, store)


def round_trips(n, multiple, *, hold=600, size=3000, start=0, now=None):
    now = now or time.time()
    out = []
    for i in range(n):
        mint, t = f"M{start + i}", now - 86400 * (i + 1)
        out.append(TradeEvent(mint, Side.BUY, t, size, 1.0))
        out.append(TradeEvent(mint, Side.SELL, t + hold, size * multiple, multiple))
    return out


def test_averaged_cost_across_scale_ins():
    events = [
        TradeEvent("A", Side.BUY, 100, 1000, 1.0),   # 1000 tokens
        TradeEvent("A", Side.BUY, 200, 1000, 2.0),   # 500 tokens
        TradeEvent("A", Side.SELL, 900, 4500, 3.0),  # all 1500
    ]
    (pos,) = reconstruct_positions(events)
    assert pos.cost_usd == pytest.approx(2000)
    assert pos.multiple == pytest.approx(2.25)


def test_open_positions_are_not_counted():
    """Unrealised gains must not count, or a wallet could look good simply by
    refusing to sell its losers."""
    events = [
        TradeEvent("A", Side.BUY, 100, 1000, 1.0),
        TradeEvent("A", Side.SELL, 200, 300, 1.5),
    ]
    assert reconstruct_positions(events) == []


def test_sell_without_observed_buy_is_ignored():
    events = [TradeEvent("A", Side.SELL, 100, 1000, 1.0)]
    assert reconstruct_positions(events) == []


def test_solid_trader_is_tracked(scorer):
    s = scorer.score("W", round_trips(30, 2.0))
    assert s.tracked and s.score > 0.5


def test_one_lucky_moonshot_is_rejected(scorer):
    """A wallet with one 100x and a pile of losses has enormous total PnL and
    is not worth following. The median multiple is what catches this - the
    mean would hide it."""
    trades = round_trips(1, 100.0, size=30_000) + round_trips(24, 0.4, start=50)
    s = scorer.score("W", trades)
    assert s.realized_pnl_usd > 1_000_000
    assert not s.tracked


def test_sniper_is_rejected(scorer):
    """Sub-minute flippers are profitable but not copyable: their edge is
    latency you do not have."""
    s = scorer.score("W", round_trips(30, 2.0, hold=5, size=8000))
    assert not s.tracked and "sniper" in s.excluded_reason


def test_bagholder_is_rejected(scorer):
    s = scorer.score("W", round_trips(30, 2.0, hold=432_000, size=8000))
    assert not s.tracked and "bagholder" in s.excluded_reason


def test_small_sample_is_rejected(scorer):
    s = scorer.score("W", round_trips(8, 3.0, size=50_000))
    assert not s.tracked and "sample_too_small" in s.excluded_reason


def test_decayed_trader_is_demoted(scorer):
    s = scorer.score("W", round_trips(60, 3.0, size=15_000))
    assert s.tracked
    s.score_7d = s.score * 0.4
    scorer.apply_decay_demotion(s)
    assert not s.tracked and "7d_decay" in s.excluded_reason


def test_score_is_bounded(scorer):
    s = scorer.score("W", round_trips(200, 50.0, size=100_000))
    assert 0.0 <= s.score <= 1.0


# --- X accounts ---------------------------------------------------------
def _seed_calls(store, handle, n, *, entry=1.0, peak=3.0, trough=0.9, graded=True):
    now = time.time()
    for i in range(n):
        cid = f"{handle}-{i}"
        store.record_x_call(cid, handle, f"MINT{i}", entry, 1.0)
        if graded:
            store.grade_x_call(cid, entry * peak, entry * trough, entry * peak * 0.8)


def test_x_cold_start_is_untrusted_not_trusted(cfg, store):
    """Before enough calls are graded, an account must NOT be trusted. The
    cold start is real and the bot has to be honest about it."""
    scorer = XAccountScorer(cfg, store)
    _seed_calls(store, "newbie", 3)
    s = scorer.score("newbie")
    assert not s.tracked
    assert s.score < 0.5
    assert "too_few_graded_calls" in s.excluded_reason


def test_x_accurate_caller_is_tracked(cfg, store):
    scorer = XAccountScorer(cfg, store)
    _seed_calls(store, "good", 20, peak=3.0, trough=0.9)
    s = scorer.score("good")
    assert s.tracked and s.hit_rate > 0.5


def test_x_caller_whose_calls_dump_first_is_rejected(cfg, store):
    """A call that 3x'd only after first dropping 70% is not a call you could
    have held. It must not count as a hit."""
    scorer = XAccountScorer(cfg, store)
    _seed_calls(store, "wicky", 20, peak=3.0, trough=0.3)
    s = scorer.score("wicky")
    assert s.hit_rate == 0.0
    assert not s.tracked


def test_x_spray_and_pray_is_rejected(cfg, store):
    scorer = XAccountScorer(cfg, store)
    _seed_calls(store, "spam", 400, peak=3.0, trough=0.9)
    s = scorer.score("spam")
    assert not s.tracked and "spray_and_pray" in s.excluded_reason


def test_x_promo_penalty_reduces_score(cfg, store):
    scorer = XAccountScorer(cfg, store)
    _seed_calls(store, "promo", 20, peak=3.0, trough=0.9)
    clean = scorer.score("promo", promo_fraction=0.0)
    paid = scorer.score("promo", promo_fraction=1.0)
    assert paid.score < clean.score
