"""Live runner and 7-day trial assessment.

These guard the operational failures that kill multi-day runs. None of them
appear in a backtest, and every one has stopped a real trial.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from brokerbot.brokers.base import AccountSummary, Broker, BrokerPosition, OrderResult
from brokerbot.brokers.paper import PaperBroker
from brokerbot.costs import PRESETS
from brokerbot.data.synthetic import random_walk
from brokerbot.live import BarStore, CycleResult, LiveRunner
from brokerbot.models import Bar, OrderStatus
from brokerbot.strategy.base import Signal, Strategy
from brokerbot.trial import TrialTracker

START = datetime(2026, 9, 15)


class AlwaysLong(Strategy):
    name = "always_long"

    def on_bar(self, symbol, history):
        return Signal(symbol, 1.0, "always")


class DeadBroker(Broker):
    name = "dead"

    async def connect(self):
        return False

    async def account(self):
        return AccountSummary(cash=0, equity=0)

    async def positions(self):
        return []

    async def place(self, order):
        return OrderResult(False, OrderStatus.REJECTED, detail="broker is down")

    async def last_price(self, symbol):
        return None


# --- bar store ------------------------------------------------------------
def test_observe_updates_todays_bar_in_place():
    store = BarStore()
    assert store.observe("X", 100.0, START) is True
    assert store.observe("X", 105.0, START) is False
    bars = store.history("X")
    assert len(bars) == 1
    assert bars[0].high == 105.0 and bars[0].close == 105.0


def test_observe_opens_a_new_bar_on_a_new_day():
    store = BarStore()
    store.observe("X", 100.0, START)
    store.observe("X", 101.0, START + timedelta(days=1))
    assert len(store.history("X")) == 2


def test_bar_store_persists(tmp_path):
    path = tmp_path / "bars.json"
    store = BarStore(path)
    store.observe("X", 100.0, START)
    store.save()
    assert len(BarStore(path).history("X")) == 1


def test_seeding_does_not_overwrite_existing_history(tmp_path):
    class Src:
        name = "s"

        def load(self, symbol, **kw):
            return random_walk(symbol, bars=50, seed=1)

        @staticmethod
        def validate(bars):
            return []

    store = BarStore()
    store._bars["X"] = [Bar("X", START, 1, 1, 1, 1)]
    store.seed("X", Src())
    assert len(store.history("X")) == 1


# --- runner ---------------------------------------------------------------
@pytest.fixture
def broker() -> PaperBroker:
    b = PaperBroker(PRESETS["nordic_equities"], starting_cash=100_000)
    b.set_price("TEST", 100.0)
    return b


def seeded_store() -> BarStore:
    store = BarStore()
    store._bars["TEST"] = random_walk("TEST", bars=100, seed=2)
    return store


async def test_dry_run_places_no_orders(broker, tmp_path):
    runner = LiveRunner(broker, AlwaysLong(), ["TEST"],
                        costs=PRESETS["nordic_equities"], bar_store=seeded_store(),
                        dry_run=True, state_dir=tmp_path)
    result = await runner.cycle()
    assert result.orders_placed == 1          # counted as intent
    assert not await broker.positions()       # but nothing actually bought


async def test_live_mode_places_orders(broker, tmp_path):
    runner = LiveRunner(broker, AlwaysLong(), ["TEST"],
                        costs=PRESETS["nordic_equities"], bar_store=seeded_store(),
                        dry_run=False, state_dir=tmp_path, max_position_weight=0.2)
    await runner.cycle()
    assert await broker.positions()


async def test_dry_run_is_the_default(broker, tmp_path):
    """A runner that sends real orders because a flag was forgotten is not an
    acceptable failure mode, even on a demo account."""
    runner = LiveRunner(broker, AlwaysLong(), ["TEST"],
                        costs=PRESETS["nordic_equities"], state_dir=tmp_path)
    assert runner.dry_run is True


async def test_connection_failure_is_reported_not_swallowed(tmp_path):
    """The single most common reason a multi-day run stops: the token expired
    and nothing said so."""
    runner = LiveRunner(DeadBroker(), AlwaysLong(), ["TEST"],
                        costs=PRESETS["zero"], state_dir=tmp_path)
    result = await runner.cycle()
    assert not result.ok
    assert not result.connected
    assert any("token" in e or "not connected" in e for e in result.errors)


async def test_heartbeat_is_written_every_cycle(broker, tmp_path):
    """'Running and finding nothing' and 'dead since Tuesday' look identical
    from outside and mean opposite things."""
    runner = LiveRunner(broker, AlwaysLong(), ["TEST"],
                        costs=PRESETS["zero"], bar_store=seeded_store(),
                        state_dir=tmp_path)
    await runner.cycle()
    beat = json.loads((Path(tmp_path) / "heartbeat.json").read_text())
    assert beat["connected"] is True
    assert "last_cycle_utc" in beat

    # The stored clock is UTC and the reader's is not. Without a local
    # rendering and a plain age beside it, someone checking a healthy run
    # against their own watch concludes it died hours ago - which is exactly
    # what this file exists to prevent.
    assert "last_cycle_local" in beat
    assert beat["age_seconds"] < 60


async def test_unexpected_broker_position_is_flagged(broker, tmp_path):
    """A position we did not open - a manual trade, or a fill we lost track of
    across a restart. Reported, never silently adopted."""
    from brokerbot.models import Order, Side
    await broker.place(Order("TEST", Side.BUY, 10))
    runner = LiveRunner(broker, AlwaysLong(), ["TEST"],
                        costs=PRESETS["zero"], bar_store=seeded_store(),
                        dry_run=True, state_dir=tmp_path)
    result = await runner.cycle()
    assert result.reconcile_drift
    assert "did not open" in result.reconcile_drift[0]


async def test_quantity_mismatch_is_flagged(broker, tmp_path):
    runner = LiveRunner(broker, AlwaysLong(), ["TEST"],
                        costs=PRESETS["zero"], bar_store=seeded_store(),
                        state_dir=tmp_path)
    runner._pending["TEST"] = 500.0           # we think we hold 500
    result = await runner.cycle()             # broker holds none
    assert any("expected 500" in d for d in result.reconcile_drift)


async def test_kill_switch_stops_the_loop(broker, tmp_path):
    runner = LiveRunner(broker, AlwaysLong(), ["TEST"],
                        costs=PRESETS["zero"], bar_store=seeded_store(),
                        state_dir=tmp_path, cycle_seconds=0.01)
    (Path(tmp_path) / "STOP").touch()
    assert await runner.run() == []


async def test_missing_price_is_recorded_not_fatal(broker, tmp_path):
    runner = LiveRunner(broker, AlwaysLong(), ["TEST", "UNKNOWN"],
                        costs=PRESETS["zero"], bar_store=seeded_store(),
                        state_dir=tmp_path)
    result = await runner.cycle()
    assert any("no price" in e for e in result.errors)
    assert result.ok


# --- trial assessment -----------------------------------------------------
def tracker_with(gaps_minutes, tmp_path, *, cycle_seconds=900, ok=True,
                 connected=True, drift=None, rejected=0):
    t = TrialTracker(tmp_path, cycle_seconds=cycle_seconds, days=7)
    ts = START
    (Path(tmp_path) / "trial_meta.json").write_text(
        json.dumps({"started_at": ts.isoformat(), "dry_run": True})
    )
    for g in gaps_minutes:
        ts += timedelta(minutes=g)
        t.record(CycleResult(ts=ts, ok=ok, connected=connected, equity=100_000,
                             reconcile_drift=drift or [], orders_rejected=rejected))
    return t


def test_healthy_week_is_operationally_sound(tmp_path):
    report = tracker_with([15] * 672, tmp_path).assess()
    assert report.uptime == pytest.approx(1.0, abs=0.01)
    assert report.operationally_sound


def test_overnight_outage_fails_the_outage_check(tmp_path):
    report = tracker_with([15] * 200 + [480] + [15] * 400, tmp_path).assess()
    assert report.longest_gap_hours == pytest.approx(8.0)
    assert report.downtime_hours > 7
    assert not report.operationally_sound
    assert "no_long_outage" in {c.name for c in report.blocking}


def test_uptime_is_gap_based_not_ratio_based(tmp_path):
    """A ratio against the configured interval reports nonsense when the real
    cadence differs - after a restart with different settings, say."""
    report = tracker_with([60] * 168, tmp_path, cycle_seconds=1.0).assess()
    assert 0.0 <= report.uptime <= 1.0


def test_run_that_died_early_fails_duration(tmp_path):
    report = tracker_with([15] * 288, tmp_path).assess()   # 3 days
    assert not report.operationally_sound
    assert "ran_long_enough" in {c.name for c in report.blocking}


def test_expired_credentials_fail_the_run(tmp_path):
    report = tracker_with([15] * 672, tmp_path, connected=False).assess()
    assert report.connection_failures > 1
    assert "credentials_held" in {c.name for c in report.blocking}


def test_position_drift_fails_the_run(tmp_path):
    report = tracker_with([15] * 672, tmp_path, drift=["TEST: mismatch"]).assess()
    assert report.drift_events > 0
    assert "positions_reconcile" in {c.name for c in report.blocking}


def test_high_rejection_rate_fails_the_run(tmp_path):
    report = tracker_with([15] * 672, tmp_path, rejected=1).assess()
    assert "orders_accepted" in {c.name for c in report.blocking}


def test_no_cycles_is_reported_clearly(tmp_path):
    report = TrialTracker(tmp_path).assess()
    assert not report.operationally_sound
    assert report.checks[0].name == "any_cycles"


def test_report_refuses_to_claim_profitability(tmp_path):
    """Seven days produces almost no closed trades. The report must say so
    rather than letting a P&L figure be read as evidence."""
    t = tracker_with([15] * 672, tmp_path)
    text = t.render(t.assess())
    assert "DOES NOT TELL YOU" in text
    assert "noise" in text
    assert "readiness" in text
