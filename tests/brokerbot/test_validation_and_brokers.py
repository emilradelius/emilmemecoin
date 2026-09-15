"""Walk-forward validation, data quality checks, and broker adapters."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from brokerbot.backtest.engine import BacktestEngine
from brokerbot.backtest.walkforward import WalkForwardValidator
from brokerbot.brokers.base import Broker
from brokerbot.brokers.etoro import EtoroBroker
from brokerbot.brokers.ibkr import IbkrBroker
from brokerbot.brokers.paper import PaperBroker
from brokerbot.brokers.saxo import SaxoBroker
from brokerbot.costs import PRESETS
from brokerbot.data.base import BarSource
from brokerbot.data.csv_source import CsvBarSource
from brokerbot.data.synthetic import random_walk
from brokerbot.models import Bar, Order, Side
from brokerbot.strategy.base import Signal
from brokerbot.strategy.library import SmaCrossover

START = datetime(2020, 1, 1)


# --- walk-forward ---------------------------------------------------------
def test_walkforward_flags_overfitting_on_pure_noise():
    """On a random walk there is no edge to find, so whatever the optimiser
    picks in-sample must fall apart out-of-sample. If this ever reports
    'robust', the validator is broken."""
    engine = BacktestEngine(PRESETS["nordic_equities"], starting_cash=100_000)
    report = WalkForwardValidator(engine, windows=5).run(
        SmaCrossover, random_walk(bars=2000, seed=3, drift=0.0),
        {"fast": [5, 10, 20, 50], "slow": [30, 60, 100, 200]},
    )
    assert report.windows
    assert not report.robust


def test_walkforward_skips_invalid_parameter_combinations():
    """SmaCrossover rejects fast >= slow; the grid contains such pairs and the
    validator must step over them rather than crash."""
    engine = BacktestEngine(PRESETS["zero"], starting_cash=100_000)
    report = WalkForwardValidator(engine, windows=3).run(
        SmaCrossover, random_walk(bars=900, seed=1),
        {"fast": [10, 50, 100], "slow": [20, 60]},
    )
    for w in report.windows:
        assert w.best_params["fast"] < w.best_params["slow"]


def test_walkforward_needs_enough_history():
    engine = BacktestEngine(PRESETS["zero"])
    report = WalkForwardValidator(engine, windows=5).run(
        SmaCrossover, random_walk(bars=30), {"fast": [5], "slow": [10]}
    )
    assert not report.windows
    assert any("not enough history" in n for n in report.notes)


# --- data validation ------------------------------------------------------
def test_clean_series_validates():
    assert BarSource.validate(random_walk(bars=200, seed=1)) == []


@pytest.mark.parametrize("bad,expected", [
    ([Bar("X", START, 10, 12, 8, 0.0)], "non-positive"),
    ([Bar("X", START, 10, 5, 20, 11)], "high below low"),
    ([Bar("X", START, 10, 12, 8, 99.0)], "outside high/low"),
])
def test_bad_bars_are_caught(bad, expected):
    assert any(expected in p for p in BarSource.validate(bad))


def test_unadjusted_split_is_caught():
    """Backtesting through an unadjusted split produces enormous fake losses
    and nothing in the report looks unusual."""
    bars = [
        Bar("X", START, 100, 101, 99, 100),
        Bar("X", START + timedelta(days=1), 25, 26, 24, 25),
    ]
    assert any("split" in p for p in BarSource.validate(bars))


def test_out_of_order_timestamps_caught():
    bars = [
        Bar("X", START + timedelta(days=1), 10, 11, 9, 10),
        Bar("X", START, 10, 11, 9, 10),
    ]
    assert any("non-monotonic" in p for p in BarSource.validate(bars))


def test_empty_series_flagged():
    assert BarSource.validate([]) == ["empty series"]


# --- csv loading ----------------------------------------------------------
def test_csv_prefers_adjusted_close(tmp_path):
    """Unadjusted prices make every split look like a crash."""
    path = tmp_path / "d.csv"
    path.write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "2020-01-02,10,11,9,10,5,1000\n"
        "2020-01-03,10,11,9,10,6,1000\n"
    )
    bars = CsvBarSource(path).load("X")
    assert [b.close for b in bars] == [5.0, 6.0]


def test_csv_skips_malformed_rows(tmp_path):
    path = tmp_path / "d.csv"
    path.write_text(
        "date,open,high,low,close\n"
        "2020-01-02,10,11,9,10\n"
        "garbage,x,y,z,w\n"
        "2020-01-03,10,11,9,12\n"
    )
    assert len(CsvBarSource(path).load("X")) == 2


def test_csv_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        CsvBarSource(tmp_path / "nope.csv").load("X")


# --- strategy contract ----------------------------------------------------
def test_signal_weight_is_clamped_long_only():
    """Shorting has borrow costs and assignment risk this framework does not
    model, so it must not be reachable by an arithmetic slip."""
    assert Signal("X", 5.0).target_weight == 1.0
    assert Signal("X", -2.0).target_weight == 0.0


def test_sma_rejects_invalid_windows():
    with pytest.raises(ValueError):
        SmaCrossover(fast=50, slow=20)


# --- brokers --------------------------------------------------------------
async def test_paper_broker_round_trip():
    b = PaperBroker(PRESETS["nordic_equities"], starting_cash=100_000)
    b.set_price("VOLV-B.ST", 250.0)
    assert await b.connect()
    assert (await b.place(Order("VOLV-B.ST", Side.BUY, 100))).ok
    assert (await b.positions())[0].quantity == 100
    assert (await b.place(Order("VOLV-B.ST", Side.SELL, 100))).ok
    acct = await b.account()
    assert acct.cash < 100_000        # costs were paid
    assert not await b.positions()


@pytest.mark.parametrize("order,expected", [
    (Order("NOPE", Side.BUY, 1), "no price"),
    (Order("VOLV-B.ST", Side.BUY, 100_000), "insufficient cash"),
    (Order("VOLV-B.ST", Side.SELL, 10), "cannot sell"),
    (Order("VOLV-B.ST", Side.BUY, 0), "non-positive"),
])
async def test_paper_broker_rejects_bad_orders(order, expected):
    b = PaperBroker(PRESETS["zero"], starting_cash=10_000)
    b.set_price("VOLV-B.ST", 250.0)
    result = await b.place(order)
    assert not result.ok
    assert expected in result.detail


def test_adapters_require_credentials():
    with pytest.raises(ValueError):
        SaxoBroker("")
    with pytest.raises(ValueError):
        EtoroBroker("")


def test_saxo_defaults_to_simulation():
    """Live trading must be an explicit choice, never a default."""
    assert "sim" in SaxoBroker("token").base
    assert SaxoBroker("token").simulation is True


def test_ibkr_defaults_to_local_gateway():
    assert "localhost" in IbkrBroker().base


def test_all_adapters_implement_the_interface():
    for cls in (PaperBroker, SaxoBroker, IbkrBroker, EtoroBroker):
        assert issubclass(cls, Broker)
        for method in ("connect", "account", "positions", "place", "last_price"):
            assert callable(getattr(cls, method))
