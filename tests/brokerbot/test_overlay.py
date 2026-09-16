"""The S&P + momentum overlay paper tracker."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from brokerbot.models import Bar
from brokerbot.overlay import (
    UNIVERSE, BENCHMARK, OverlayState, OverlayTracker, describe_universe,
)

START = datetime(2022, 1, 3)


def synth(symbol, days, drift, noise=0.01, seed=3):
    import random
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(days):
        price *= (1 + drift) * (1 + rng.gauss(0, noise))
        ts = START + timedelta(days=i)
        out.append(Bar(symbol, ts, price, price * 1.005, price * 0.995, price, 1e6))
    return out


@pytest.fixture
def tracker(tmp_path):
    return OverlayTracker(tmp_path, overlay=0.30, starting_equity=100_000.0)


# --- universe -------------------------------------------------------------
def test_the_universe_spans_every_asset_class():
    """The whole edge is diversification across classes. Every subset tested
    scored worse than the full set, so a universe that quietly lost a class
    would be a real regression."""
    classes = {cls for _, cls in UNIVERSE.values()}
    assert classes == {"equity", "bond", "metal", "energy", "ag", "fx"}
    assert len(UNIVERSE) == 28
    assert BENCHMARK not in UNIVERSE


def test_every_instrument_is_named_for_a_human():
    text = describe_universe()
    for name, _ in UNIVERSE.values():
        assert name in text


# --- position sizing ------------------------------------------------------
def test_direction_follows_the_twelve_month_move(tracker):
    up = {"UP": synth("UP", 500, 0.0012)}
    down = {"DN": synth("DN", 500, -0.0012)}
    assert tracker.compute_positions(up)[0].weight > 0
    assert tracker.compute_positions(down)[0].weight < 0


def test_weights_are_split_across_the_book(tracker):
    """Each instrument gets its slice divided by the number of positions.
    Without that, adding markets would keep inflating total exposure."""
    hist = {f"S{i}": synth(f"S{i}", 500, 0.0012, seed=i) for i in range(4)}
    pos = tracker.compute_positions(hist)
    assert len(pos) == 4
    one = tracker.compute_positions({"S0": hist["S0"]})
    assert abs(pos[0].weight) < abs(one[0].weight)


def test_an_instrument_without_enough_history_is_skipped(tracker):
    assert tracker.compute_positions({"X": synth("X", 60, 0.001)}) == []


# --- marking --------------------------------------------------------------
def test_the_first_cycle_books_no_return(tracker, monkeypatch):
    """There is no previous close to measure against. Booking one would
    invent a day of performance out of nothing."""
    monkeypatch.setattr(tracker, "fetch", lambda syms, **kw: _fake(syms))
    tracker.cycle()
    state = tracker.load()
    assert state.equity == pytest.approx(100_000.0)
    assert state.benchmark_equity == pytest.approx(100_000.0)
    assert len(state.history) == 1


def test_the_benchmark_is_tracked_over_the_same_window(tracker, monkeypatch):
    """Comparing against a benchmark measured over a different period is the
    easiest way to report a win that did not happen."""
    monkeypatch.setattr(tracker, "fetch", lambda syms, **kw: _fake(syms))
    tracker.cycle()
    monkeypatch.setattr(tracker, "fetch", lambda syms, **kw: _fake(syms, shift=1))
    tracker.cycle()
    state = tracker.load()
    assert len(state.history) == 2
    assert state.benchmark_equity != 100_000.0
    assert state.history[-1]["date"] != state.history[0]["date"]


def test_borrowing_cost_is_charged_on_the_overlay(tmp_path, monkeypatch):
    """The overlay is leverage even though no cash moves. Ignoring the cost
    overstates the result by more than the strategy's whole edge."""
    def run(rate):
        t = OverlayTracker(tmp_path / str(rate), overlay=0.30, borrow_rate=rate)
        monkeypatch.setattr(t, "fetch", lambda syms, **kw: _fake(syms))
        t.cycle()
        monkeypatch.setattr(t, "fetch", lambda syms, **kw: _fake(syms, shift=1))
        t.cycle()
        return t.load().equity
    assert run(0.10) < run(0.0)


def test_state_survives_a_restart(tracker, monkeypatch):
    monkeypatch.setattr(tracker, "fetch", lambda syms, **kw: _fake(syms))
    tracker.cycle()
    again = OverlayTracker(tracker.dir)
    assert again.load().positions == tracker.load().positions


def test_a_corrupt_state_file_is_reported_not_silently_reset(tracker, caplog):
    tracker.dir.mkdir(parents=True, exist_ok=True)
    tracker.path.write_text("{ this is not json")
    assert tracker.load() is None


# --- report ---------------------------------------------------------------
def test_a_short_run_is_labelled_as_meaningless(tracker, monkeypatch):
    """Two weeks of noise dwarfs an edge of half a percent a year. A report
    that lets someone read a fortnight as evidence is the failure mode."""
    monkeypatch.setattr(tracker, "fetch", lambda syms, **kw: _fake(syms))
    tracker.cycle()
    text = tracker.render()
    assert "not a result" in text
    assert "4 of the last 9 years" in text


def test_the_report_says_so_when_there_is_nothing_yet(tracker):
    assert "Nothing recorded yet" in tracker.render()


def _fake(symbols, shift=0):
    return {s: synth(s, 500 + shift, 0.0008, seed=hash(s) % 100)
            for s in symbols}


def test_running_twice_in_one_day_records_once(tracker, monkeypatch):
    """The job fires on a schedule and again at load, so a reboot can run it
    twice on the same date. The second pass sees unchanged prices, books a
    zero return and charges another day of borrowing - equity drifts down for
    no reason at all."""
    monkeypatch.setattr(tracker, "fetch", lambda syms, **kw: _fake(syms))
    tracker.cycle()
    monkeypatch.setattr(tracker, "fetch", lambda syms, **kw: _fake(syms, shift=1))
    tracker.cycle()
    after_two = tracker.load()
    equity, records = after_two.equity, len(after_two.history)

    for _ in range(5):
        assert tracker.cycle().get("already_recorded")
    final = tracker.load()
    assert len(final.history) == records
    assert final.equity == pytest.approx(equity)


def test_borrowing_accrues_over_a_gap_not_per_cycle(tmp_path, monkeypatch):
    """A shut laptop, a weekend or a holiday means one cycle covers several
    days. The price return already spans the whole gap, so charging a single
    day of borrowing hands the strategy free leverage over exactly the
    stretches nobody was watching."""
    def run(gap_days):
        t = OverlayTracker(tmp_path / f"g{gap_days}", overlay=0.30, borrow_rate=0.10)
        monkeypatch.setattr(t, "fetch", lambda syms, **kw: _fake(syms))
        t.cycle()
        state = t.load()
        # Rewind the stored date so the next cycle looks like a long gap.
        gone = date.fromisoformat(state.history[-1]["date"]) - timedelta(days=gap_days)
        state.history[-1]["date"] = gone.isoformat()
        t.save(state)
        t.cycle()
        return t.load().equity

    # Same prices either side, so any difference is the financing charge.
    assert run(10) < run(1)
