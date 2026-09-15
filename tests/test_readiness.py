"""The go-live gate.

These tests encode the distinction that matters most here: a profitable paper
record is not the same as evidence of an edge. The headline case is that a
history making +11 SOL can be rejected while one making +1.4 SOL passes,
because the first one's profit came from a single trade.
"""

from __future__ import annotations

import time

import pytest

from memebot.models import ExitReason, Position, Source, TraderScore
from memebot.readiness import ReadinessAssessor


def seed(store, multiples, *, days=35, tokens=None, with_x=True, size=0.25):
    now = time.time()
    if with_x:
        t = TraderScore(actor_id="caller", source=Source.X, score=0.7, tracked=True)
        t.graded_calls = 24
        store.upsert_score(t)
    n = len(multiples)
    for i, mult in enumerate(multiples):
        p = Position(
            token_mint=f"M{i % tokens if tokens else i}", token_symbol=f"T{i}",
            entry_price_usd=0.001, size_sol=size, mode="paper",
            opened_at=now - days * 86400 + i * (days * 86400 / max(n, 1)),
        )
        p.realized_pnl_sol = size * (mult - 1.0)
        p.closed_at = p.opened_at + 3600
        p.close_reason = ExitReason.TAKE_PROFIT if mult > 1 else ExitReason.STOP_LOSS
        store.save_position(p)
    return store


def skewed(n):
    """A realistic meme-coin return profile: mostly small losses, a few big
    winners. The median trade loses money and the strategy still works."""
    pattern = [0.55, 0.6, 0.5, 1.2, 0.65, 2.4, 0.55, 1.1, 0.6, 8.0]
    return [pattern[i % len(pattern)] for i in range(n)]


@pytest.fixture
def assessor(cfg, store) -> ReadinessAssessor:
    return ReadinessAssessor(cfg, store)


def test_no_history_is_not_ready(assessor):
    r = assessor.assess()
    assert not r.ready
    assert r.checks[0].name == "any_history"


def test_short_profitable_run_is_rejected(assessor, store):
    """The pattern behind most 'my AI bot made me money' posts: a handful of
    trades over a few days that happened to include one big winner."""
    seed(store, [0.6, 0.5, 12.0, 0.7, 0.55, 0.6, 1.3, 0.5], days=6)
    r = assessor.assess()
    assert not r.ready
    assert r.total_pnl_sol > 0          # it *was* profitable
    blocked = {c.name for c in r.blocking}
    assert {"sample_size", "calendar_time"} <= blocked


def test_one_lucky_trade_is_rejected_despite_large_profit(assessor, store):
    """The same guard the bot applies to other wallets, applied to yourself.
    Rejecting a trader for this pattern and then going live on it would be
    incoherent."""
    seed(store, [0.55] * 29 + [60.0], days=40)
    r = assessor.assess()
    assert r.total_pnl_sol > 10          # very profitable on paper
    assert not r.ready
    assert "not_one_lucky_trade" in {c.name for c in r.blocking}


def test_positive_skew_strategy_passes(assessor, store):
    """The critical correctness property: meme coins are a positive-skew asset
    class. Most trades lose. Gating on the median trade would reject every
    strategy that actually works here."""
    seed(store, skewed(45), days=35)
    r = assessor.assess()
    assert r.median_multiple < 1.0       # the typical trade loses money
    assert r.profit_factor >= 1.3
    assert r.ready


def test_smaller_but_broader_profit_beats_one_big_win(assessor, cfg, store, tmp_path):
    from memebot.store import Store
    lucky = seed(Store(tmp_path / "a.db"), [0.55] * 29 + [60.0], days=40)
    broad = seed(Store(tmp_path / "b.db"), skewed(45), days=35)
    lucky_r = ReadinessAssessor(cfg, lucky).assess()
    broad_r = ReadinessAssessor(cfg, broad).assess()
    assert lucky_r.total_pnl_sol > broad_r.total_pnl_sol
    assert not lucky_r.ready and broad_r.ready
    lucky.close(); broad.close()


def test_losing_strategy_is_rejected(assessor, store):
    seed(store, [0.6] * 40 + [1.1] * 10, days=35)
    r = assessor.assess()
    assert not r.ready
    assert {"profit_factor", "profitable"} <= {c.name for c in r.blocking}


def test_concentrated_tokens_rejected(assessor, store):
    seed(store, [1.5, 0.6, 2.0, 0.7, 1.8] * 8, days=30, tokens=5)
    r = assessor.assess()
    assert not r.ready
    assert "token_diversity" in {c.name for c in r.blocking}


def test_early_losing_streak_shows_as_drawdown(assessor, store):
    """Measuring drawdown against cumulative profit alone reports 0% for a
    strategy that bleeds for weeks before recovering - exactly the run most
    likely to make someone switch the bot off."""
    seed(store, [0.4] * 15 + [5.0] * 20, days=30)
    r = assessor.assess()
    assert r.max_drawdown_pct > 0


def test_ungraded_x_scores_block_go_live(cfg, store):
    """Until X calls have been graded, the paper record was produced by a bot
    running on placeholder weights - a different bot than the live one."""
    seed(store, skewed(45), days=35, with_x=False)
    r = ReadinessAssessor(cfg, store).assess()
    assert not r.ready
    assert "x_scores_learned" in {c.name for c in r.blocking}


def test_tripped_breaker_blocks_go_live(assessor, store):
    seed(store, skewed(45), days=35)
    store.kv_set("breaker_state", {"halted": True, "halt_reason": "test"})
    r = assessor.assess()
    assert not r.ready
    assert "breakers_clear" in {c.name for c in r.blocking}


def test_recommended_size_is_a_fraction_of_paper_size(assessor, store, cfg):
    seed(store, skewed(45), days=35)
    r = assessor.assess()
    assert r.ready
    assert 0 < r.recommended_live_size_sol < cfg.get("execution.base_position_sol")


def test_render_is_readable_in_both_modes(assessor, store):
    seed(store, skewed(45), days=35)
    r = assessor.assess()
    assert "VERDICT" in assessor.render(r)
    assert "<b>" in assessor.render(r, html=True)


def test_profit_factor_handles_no_losses(assessor, store):
    seed(store, [2.0] * 35, days=30)
    r = assessor.assess()
    assert r.profit_factor == float("inf")
