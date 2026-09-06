"""Exit triggers and execution mode locks."""

from __future__ import annotations

import time

import pytest

from memebot.enrich.dexscreener import DexScreener
from memebot.enrich.rugcheck import RugCheck
from memebot.execution.manager import ExecutionManager, NullExecutor
from memebot.exits import ExitMonitor
from memebot.models import ExitReason, Position, Side, Signal, Source
from memebot.scoring.safety import SafetyGate


@pytest.fixture
def monitor(cfg, store) -> ExitMonitor:
    dex = DexScreener()
    return ExitMonitor(cfg, store, dex, SafetyGate(cfg, dex, RugCheck()))


def position(entry=0.001, age_hours=1.0, peak=0.0, rungs=None, triggers=None):
    return Position(
        token_mint="M", token_symbol="X", entry_price_usd=entry, size_sol=0.25,
        opened_at=time.time() - age_hours * 3600, peak_price_usd=peak,
        ladder_rungs_hit=rungs or [], trigger_actors=triggers or [],
    )


def test_stop_loss_fires_past_threshold(monitor):
    assert monitor.check_stop_loss(position(), 0.0005) is not None
    assert monitor.check_stop_loss(position(), 0.0008) is None


def test_take_profit_ladder_is_partial_and_progresses(monitor):
    first = monitor.check_take_profit(position(), 0.002)
    assert first.fraction < 1.0
    assert monitor.check_take_profit(position(rungs=[2.0]), 0.002) is None
    assert monitor.check_take_profit(position(rungs=[2.0]), 0.004) is not None


def test_trailing_stop_only_after_first_rung(monitor):
    """Before any profit is banked the plain stop governs; trailing this
    early would shake you out on normal volatility."""
    assert monitor.check_trailing_stop(position(peak=0.005), 0.002) is None
    assert monitor.check_trailing_stop(position(peak=0.005, rungs=[2.0]), 0.002) is not None


def test_time_stop(monitor):
    assert monitor.check_time_stop(position(age_hours=25), 0.0011) is not None
    assert monitor.check_time_stop(position(age_hours=2), 0.0011) is None


def test_smart_money_exit_needs_a_quorum(monitor):
    def sells(n):
        return [
            Signal(source=Source.PUMPFUN, actor_id=f"W{i}", token_mint="M",
                   side=Side.SELL)
            for i in range(n)
        ]

    triggers = ["W0", "W1", "W2"]
    assert monitor.check_smart_money(position(triggers=triggers), sells(2)) is not None
    assert monitor.check_smart_money(position(triggers=triggers), sells(1)) is None


def test_social_sells_do_not_trigger_smart_money_exit(monitor):
    """Only on-chain exits by the triggering cohort count - a tweet is not a
    position change."""
    social = [
        Signal(source=Source.X, actor_id="W0", token_mint="M", side=Side.SELL),
        Signal(source=Source.X, actor_id="W1", token_mint="M", side=Side.SELL),
    ]
    assert monitor.check_smart_money(position(triggers=["W0", "W1"]), social) is None


def test_liquidity_collapse_is_urgent(monitor):
    pos = position()
    monitor.remember_entry_liquidity(pos.id, 100_000)
    decision = monitor.check_safety_regression(pos, 30_000, None)
    assert decision.urgent
    assert decision.reason is ExitReason.LIQUIDITY_COLLAPSE


def test_partial_exit_updates_remaining_fraction(monitor):
    pos = position()
    decision = monitor.check_take_profit(pos, 0.002)
    monitor.apply_partial(pos, decision, 0.002)
    assert 2.0 in pos.ladder_rungs_hit
    assert pos.remaining_fraction == pytest.approx(0.6)
    assert pos.is_open


def test_full_exit_closes_position(monitor):
    pos = position()
    decision = monitor.check_stop_loss(pos, 0.0004)
    monitor.apply_partial(pos, decision, 0.0004)
    assert not pos.is_open
    assert pos.close_reason is ExitReason.STOP_LOSS


# --- execution modes ------------------------------------------------------
@pytest.fixture
def manager(cfg, store) -> ExecutionManager:
    cfg.set("execution.mode", "alerts")
    return ExecutionManager(cfg, store, DexScreener())


async def test_live_mode_refused_without_arming(manager, monkeypatch):
    """The critical safety property: Telegram can stop trading but can never
    start it."""
    monkeypatch.setenv("TRADING_WALLET_PRIVATE_KEY", "fake")
    manager.cfg.set("execution.live_mode_armed", False)
    reply = await manager.set_mode("live")
    assert "Cannot switch to live" in reply
    assert manager.mode != "live"


async def test_live_mode_refused_without_key(manager, monkeypatch):
    monkeypatch.delenv("TRADING_WALLET_PRIVATE_KEY", raising=False)
    manager.cfg.set("execution.live_mode_armed", True)
    reply = await manager.set_mode("live")
    assert "Cannot switch to live" in reply
    assert manager.mode != "live"


async def test_paper_and_alerts_modes_switch_freely(manager):
    await manager.set_mode("paper")
    assert manager.mode == "paper" and manager.trades
    await manager.set_mode("alerts")
    assert manager.mode == "alerts" and not manager.trades


async def test_panic_drops_to_alerts_and_halts(manager):
    await manager.set_mode("paper")
    await manager.panic()
    assert manager.mode == "alerts"
    assert isinstance(manager.executor, NullExecutor)
    assert not manager.guardrails.can_open(open_positions=0)[0]


async def test_mode_survives_restart(cfg, store):
    cfg.set("execution.mode", "alerts")
    m1 = ExecutionManager(cfg, store, DexScreener())
    await m1.set_mode("paper")
    m2 = ExecutionManager(cfg, store, DexScreener())
    assert m2.mode == "paper"


async def test_alerts_mode_places_no_orders(manager):
    result = await manager.executor.buy("M", 0.25)
    assert not result.ok
