"""The Yahoo quote feed and the paper broker's use of it.

Nothing here touches the network. A test that needs the internet to pass
fails on a train, and a test suite you learn to ignore is worse than none.
The payload shapes below are trimmed copies of real responses, including the
null-padded rows Yahoo emits for exchange holidays.
"""

from __future__ import annotations

import urllib.error

import pytest

from brokerbot.brokers.paper import PaperBroker
from brokerbot.costs import PRESETS
from brokerbot.data.base import BarSource
from brokerbot.data.yahoo import YahooBarSource, YahooError
from brokerbot.models import Order, Side

# Three trading days and one holiday, the way Yahoo actually sends it.
PAYLOAD = {
    "chart": {
        "error": None,
        "result": [{
            "meta": {
                "symbol": "VOLV-B.ST",
                "currency": "SEK",
                "regularMarketPrice": 330.1,
            },
            "timestamp": [1756857600, 1756944000, 1757203200, 1757289600],
            "indicators": {
                "quote": [{
                    "open": [338.7, 348.3, None, 350.0],
                    "high": [349.6, 349.0, None, 352.0],
                    "low": [334.2, 343.1, None, 349.0],
                    "close": [348.3, 348.5, None, 351.0],
                    "volume": [4476192, 2386412, None, 1_000_000],
                }],
                "adjclose": [{"adjclose": [348.3, 348.5, None, 351.0]}],
            },
        }],
    }
}


def _source(payload=PAYLOAD) -> YahooBarSource:
    source = YahooBarSource()
    source._fetch_sync = lambda symbol, **kw: YahooBarSource._unwrap(symbol, payload)
    return source


# --- bars -----------------------------------------------------------------
def test_null_rows_are_dropped_not_carried_forward():
    """Yahoo sends an all-null row for exchange holidays. Filling it forward
    would invent a bar that never traded, and the strategy would act on it."""
    bars = _source().load("VOLV-B.ST")
    assert len(bars) == 3
    assert all(bar.close > 0 for bar in bars)


def test_bars_are_sorted_and_survive_the_quality_validator():
    bars = _source().load("VOLV-B.ST")
    assert [b.ts for b in bars] == sorted(b.ts for b in bars)
    assert BarSource.validate(bars) == []


def test_adjusted_close_rescales_the_whole_bar():
    """A 4:1 split halves nothing if only the close is adjusted - the bar ends
    up with an adjusted close sitting outside its own unadjusted low, which
    the validator rejects and the engine would otherwise fill against."""
    split = {"chart": {"error": None, "result": [{
        "meta": {"symbol": "X", "currency": "USD", "regularMarketPrice": 25.0},
        "timestamp": [1756857600],
        "indicators": {
            "quote": [{"open": [100.0], "high": [104.0], "low": [96.0],
                       "close": [100.0], "volume": [10.0]}],
            "adjclose": [{"adjclose": [25.0]}],
        },
    }]}}
    bar = _source(split).load("X")[0]
    assert bar.close == pytest.approx(25.0)
    assert bar.open == pytest.approx(25.0)
    assert bar.high == pytest.approx(26.0)
    assert bar.low == pytest.approx(24.0)
    assert bar.low <= bar.close <= bar.high
    assert BarSource.validate([bar]) == []


# --- quotes ---------------------------------------------------------------
def test_quote_reports_the_currency_it_actually_got():
    """The account is in SEK. A symbol that answers in USD must say so rather
    than handing back a bare number that looks entirely reasonable."""
    source = _source()
    price, currency = source.quote("VOLV-B.ST")
    assert price == pytest.approx(330.1)
    assert currency == "SEK"
    assert source.currency_of("VOLV-B.ST") == "SEK"


def test_quote_falls_back_to_the_last_close_out_of_hours():
    """Some instruments drop regularMarketPrice outside the session."""
    closed = {"chart": {"error": None, "result": [{
        "meta": {"symbol": "VOLV-B.ST", "currency": "SEK"},
        "timestamp": [1756857600],
        "indicators": {
            "quote": [{"open": [338.7], "high": [349.6], "low": [334.2],
                       "close": [348.3], "volume": [1.0]}],
            "adjclose": [{"adjclose": [348.3]}],
        },
    }]}}
    price, currency = _source(closed).quote("VOLV-B.ST")
    assert price == pytest.approx(348.3)
    assert currency == "SEK"


def test_quote_returns_none_rather_than_raising_on_a_dead_feed():
    """A failed quote must not take the runner's whole cycle down with it."""
    source = YahooBarSource()

    def boom(symbol, **kw):
        raise YahooError("network is on fire")

    source._fetch_sync = boom
    assert source.quote("VOLV-B.ST") is None


def test_unknown_symbol_says_what_is_wrong_with_it(monkeypatch):
    """Yahoo answers 404 for a ticker missing its exchange suffix. 'VOLVB' is
    the mistake a Swedish user will actually make, so the message names the
    fix instead of reprinting an HTTP code."""
    def not_found(request, timeout=None):
        raise urllib.error.HTTPError(
            url=request.full_url, code=404, msg="Not Found", hdrs=None, fp=None
        )

    monkeypatch.setattr("brokerbot.data.yahoo.urllib.request.urlopen", not_found)
    with pytest.raises(YahooError, match=r"VOLV-B\.ST"):
        YahooBarSource().load("VOLVB")


def test_a_server_error_is_reported_as_transport_not_as_a_bad_symbol(monkeypatch):
    """Telling someone their ticker is wrong when Yahoo is down sends them
    looking in the wrong place."""
    def server_error(request, timeout=None):
        raise urllib.error.HTTPError(
            url=request.full_url, code=503, msg="", hdrs=None, fp=None
        )

    monkeypatch.setattr("brokerbot.data.yahoo.urllib.request.urlopen", server_error)
    with pytest.raises(YahooError, match="503"):
        YahooBarSource().load("VOLV-B.ST")


def test_empty_result_is_an_error_not_an_empty_list():
    """Returning [] here would look like 'this instrument has no history',
    and a strategy would wait patiently forever for a warmup that never comes."""
    with pytest.raises(YahooError):
        YahooBarSource._unwrap("X", {"chart": {"result": [], "error": None}})


# --- paper broker integration ---------------------------------------------
class FakeQuotes:
    name = "fake"

    def __init__(self, *prices):
        self.prices = list(prices)
        self.calls = 0

    async def last_price(self, symbol):
        self.calls += 1
        return self.prices.pop(0) if self.prices else None


async def test_paper_broker_without_quotes_is_unchanged():
    """The existing contract: no feed, no price, orders rejected."""
    broker = PaperBroker(PRESETS["nordic_equities"])
    assert await broker.last_price("VOLV-B.ST") is None


async def test_paper_broker_pulls_a_price_from_the_feed():
    broker = PaperBroker(PRESETS["nordic_equities"], quotes=FakeQuotes(330.1))
    assert await broker.last_price("VOLV-B.ST") == pytest.approx(330.1)


async def test_the_fetched_price_is_the_price_the_order_fills_at():
    """The signal is computed from last_price, so the fill must use the same
    number. A fresh quote between signal and fill is look-ahead."""
    broker = PaperBroker(PRESETS["nordic_equities"], quotes=FakeQuotes(330.1))
    price = await broker.last_price("VOLV-B.ST")
    result = await broker.place(Order("VOLV-B.ST", Side.BUY, quantity=10))
    assert result.ok
    assert result.avg_fill_price == pytest.approx(price)


async def test_a_failed_quote_falls_back_to_the_last_known_price():
    """One bad poll should not delete the symbol from the run."""
    broker = PaperBroker(PRESETS["nordic_equities"], quotes=FakeQuotes(330.1, None))
    assert await broker.last_price("VOLV-B.ST") == pytest.approx(330.1)
    assert await broker.last_price("VOLV-B.ST") == pytest.approx(330.1)


async def test_a_raising_quote_source_does_not_escape_the_broker():
    class Exploding:
        async def last_price(self, symbol):
            raise RuntimeError("boom")

    broker = PaperBroker(PRESETS["nordic_equities"], quotes=Exploding())
    broker.set_price("VOLV-B.ST", 300.0)
    assert await broker.last_price("VOLV-B.ST") == pytest.approx(300.0)


async def test_paper_broker_stays_marked_unfit_for_live_orders():
    """A free, undocumented feed is fine for rehearsal and must never be the
    thing that sizes real money."""
    assert PaperBroker.supports_live is False
