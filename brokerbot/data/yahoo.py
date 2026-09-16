"""Free daily bars and quotes from Yahoo's chart endpoint.

This exists so the paper broker has somewhere to get a price. Without it
`preflight --broker paper` fails at the first symbol and the whole live path -
orders, reconciliation, the trial report - can only be exercised by opening a
real broker account first.

Two things are worth knowing before trusting anything it returns.

**It is an undocumented endpoint.** There is no contract, no deprecation
notice and no support. It changes when Yahoo feels like it. That is acceptable
for a paper run, where a bad day costs nothing, and not acceptable as the
price source for live orders - a broker quote is the only thing that should
ever size a real trade. `supports_live` stays ``False`` on the paper broker
for exactly this reason.

**Reproducibility is the trade-off.** A backtest keyed to a CSV you exported
once stays the same forever; one keyed to this changes underneath you when
Yahoo revises history. Use :class:`~brokerbot.data.csv_source.CsvBarSource`
for anything you intend to compare against later.

Adjusted closes are requested and preferred, and the rest of the bar is
rescaled by the same factor, because the alternative is that every share split
arrives as a 50% crash the strategy looks clever for having dodged.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from ..models import Bar
from .base import BarSource

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

# Yahoo returns 404 to the default urllib user agent.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; brokerbot/1.0)"}


class YahooError(RuntimeError):
    """Raised when the endpoint answers with something unusable."""


class YahooBarSource(BarSource):
    """Daily OHLCV bars and last-traded prices for listed instruments.

    Symbols are Yahoo's own: ``VOLV-B.ST`` (Stockholm), ``AAPL`` (US),
    ``NOVO-B.CO`` (Copenhagen). A symbol the exchange suffix is missing from
    resolves to the US listing or to nothing at all, which is why
    :meth:`quote` reports the currency it actually got - see
    :meth:`currency_of`.
    """

    name = "yahoo"

    def __init__(self, *, timeout: float = 20.0, session: Any = None) -> None:
        self.timeout = timeout
        self._session = session          # aiohttp session, created on demand
        self._currency: dict[str, str] = {}

    # ------------------------------------------------------------------ fetch

    def _url(self, symbol: str, *, range_: str, interval: str) -> str:
        query = urllib.parse.urlencode({
            "range": range_,
            "interval": interval,
            "includeAdjustedClose": "true",
        })
        return f"{CHART_URL}{urllib.parse.quote(symbol)}?{query}"

    def _fetch_sync(self, symbol: str, *, range_: str, interval: str) -> dict:
        request = urllib.request.Request(
            self._url(symbol, range_=range_, interval=interval), headers=_HEADERS
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 404 is the answer for an unknown symbol, not a transport failure,
            # and saying so saves a lot of staring at a stack trace.
            if exc.code == 404:
                raise YahooError(
                    f"Yahoo does not know the symbol {symbol!r}. Stockholm "
                    f"tickers need the .ST suffix, e.g. VOLV-B.ST."
                ) from exc
            raise YahooError(f"Yahoo returned HTTP {exc.code} for {symbol}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise YahooError(f"Yahoo request for {symbol} failed: {exc}") from exc
        return self._unwrap(symbol, payload)

    @staticmethod
    def _unwrap(symbol: str, payload: dict) -> dict:
        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise YahooError(f"Yahoo error for {symbol}: {chart['error']}")
        results = chart.get("result") or []
        if not results:
            raise YahooError(f"Yahoo returned no data for {symbol}")
        return results[0]

    # ------------------------------------------------------------------- bars

    def load(self, symbol: str, **kwargs) -> list[Bar]:
        """Return daily bars for ``symbol``, oldest first.

        ``range`` accepts Yahoo's vocabulary (``1mo``, ``1y``, ``5y``,
        ``max``); ``interval`` defaults to ``1d``. Intraday intervals are only
        served for recent ranges, which the endpoint enforces, not this code.
        """
        range_ = str(kwargs.get("range", "2y"))
        interval = str(kwargs.get("interval", "1d"))
        result = self._fetch_sync(symbol, range_=range_, interval=interval)
        self._remember_currency(symbol, result)
        return self._to_bars(symbol, result)

    @staticmethod
    def _to_bars(symbol: str, result: dict) -> list[Bar]:
        stamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quote = (indicators.get("quote") or [{}])[0]
        adjusted = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []

        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        bars: list[Bar] = []
        gaps = 0

        for i, stamp in enumerate(stamps):
            close = _at(closes, i)
            # Yahoo emits a null row for exchange holidays and halted days.
            # Carrying those forward would invent bars that never traded.
            if stamp is None or close is None or close <= 0:
                gaps += 1
                continue

            factor = 1.0
            adj = _at(adjusted, i)
            if adj is not None and adj > 0:
                factor = adj / close
                close = adj

            open_ = _at(opens, i)
            high = _at(highs, i)
            low = _at(lows, i)
            if open_ is None or high is None or low is None:
                gaps += 1
                continue

            bars.append(Bar(
                symbol=symbol,
                ts=datetime.fromtimestamp(stamp),
                open=open_ * factor,
                high=high * factor,
                low=low * factor,
                close=close,
                volume=float(_at(volumes, i) or 0.0),
            ))

        if gaps:
            log.debug("%s: skipped %d empty bar(s) from Yahoo", symbol, gaps)

        bars.sort(key=lambda b: b.ts)
        return bars

    # ----------------------------------------------------------------- quotes

    def quote(self, symbol: str) -> tuple[float, str] | None:
        """Return ``(price, currency)`` for ``symbol``, or ``None``.

        The currency comes back with the price rather than being assumed,
        because ``AAPL`` and ``VOLV-B.ST`` both answer happily and one of them
        is not in your account's currency.
        """
        try:
            result = self._fetch_sync(symbol, range_="1d", interval="1d")
        except YahooError as exc:
            log.warning("quote for %s failed: %s", symbol, exc)
            return None

        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        currency = str(meta.get("currency") or "")
        if price is None:
            # Out of hours some instruments drop the live field; the last
            # close is the honest answer and is what a paper fill should use.
            bars = self._to_bars(symbol, result)
            if not bars:
                return None
            price = bars[-1].close

        try:
            price = float(price)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None

        if currency:
            self._currency[symbol] = currency
        return price, currency

    def currency_of(self, symbol: str) -> str | None:
        """Currency last seen for ``symbol``, or ``None`` if never fetched."""
        return self._currency.get(symbol)

    def _remember_currency(self, symbol: str, result: dict) -> None:
        currency = (result.get("meta") or {}).get("currency")
        if currency:
            self._currency[symbol] = str(currency)

    async def last_price(self, symbol: str) -> float | None:
        """Async face of :meth:`quote`, for the broker interface.

        The fetch is blocking, so it runs on a worker thread; a stalled HTTP
        call must not freeze the runner's whole cycle.
        """
        import asyncio

        result = await asyncio.to_thread(self.quote, symbol)
        return None if result is None else result[0]

    async def close(self) -> None:
        return None


def _at(values: list, i: int):
    """``values[i]`` when it exists and is not null, else ``None``.

    Yahoo pads its arrays inconsistently; indexing them directly raises on a
    perfectly ordinary response.
    """
    if i >= len(values):
        return None
    value = values[i]
    return None if value is None else float(value)
