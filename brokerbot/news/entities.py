"""Mapping headlines to tickers.

The same problem as resolving ``$TICKER`` to a mint address in the meme coin
bot, and just as consequential: acting on a story matched to the wrong company
is worse than missing it entirely.

Two failure modes pull in opposite directions, and the design leans hard
toward the first:

* **False positives.** Many company names are ordinary words - Gap, Ford,
  Shell, Apple, Sandvik. "Investors shell out for bonds" must not match Shell.
  Short and word-like names therefore require stricter evidence.
* **False negatives.** Missing a match costs one trade you would have made.
  Cheap, by comparison.

Evidence is ranked: an explicit ticker beats a full legal name, which beats a
common short name. Anything resolving to several companies is dropped rather
than guessed - a coin-flip between two tickers is not a signal.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Instrument:
    symbol: str
    name: str
    aliases: list[str] = field(default_factory=list)
    ambiguous_name: bool = False
    """True when the company name is also an ordinary word. Such names match
    only alongside a corroborating financial term."""


# Terms that mark a headline as being about a company rather than using the
# word in its everyday sense.
FINANCIAL_CONTEXT = re.compile(
    r"\b(shares?|stock|earnings|revenue|profit|loss|quarter(?:ly)?|q[1-4]|guidance"
    r"|dividend|forecast|outlook|analyst|upgrade|downgrade|acquisition|merger"
    r"|ceo|cfo|ipo|buyback|results|sales|margin|ebit(?:da)?|kv[1-4]|rapport"
    r"|vinst|omsättning|utdelning|aktie(?:n|r)?)\b",
    re.I,
)

TICKER_RE = re.compile(r"\$([A-Z]{1,6}(?:[-.][A-Z]{1,3})?)\b")


@dataclass(slots=True)
class EntityMatch:
    symbol: str
    confidence: float
    method: str
    """``explicit_ticker``, ``full_name`` or ``alias``."""


class EntityResolver:
    def __init__(self, instruments: list[Instrument] | None = None) -> None:
        self.instruments: dict[str, Instrument] = {}
        for inst in instruments or DEFAULT_UNIVERSE:
            self.add(inst)

    def add(self, inst: Instrument) -> None:
        self.instruments[inst.symbol] = inst

    @staticmethod
    def _contains_word(haystack: str, needle: str) -> bool:
        return re.search(rf"\b{re.escape(needle.lower())}\b", haystack) is not None

    def resolve(self, text: str) -> list[EntityMatch]:
        """All companies plausibly referenced, strongest evidence first."""
        lowered = text.lower()
        has_context = bool(FINANCIAL_CONTEXT.search(text))
        matches: dict[str, EntityMatch] = {}

        # 1. Explicit $TICKER - unambiguous.
        for raw in TICKER_RE.findall(text):
            if raw in self.instruments:
                matches[raw] = EntityMatch(raw, 1.0, "explicit_ticker")

        for symbol, inst in self.instruments.items():
            if symbol in matches:
                continue

            # 2. Bare ticker as a standalone uppercase token.
            base = symbol.split(".")[0]
            if len(base) >= 3 and re.search(rf"\b{re.escape(base)}\b", text):
                matches[symbol] = EntityMatch(symbol, 0.9, "explicit_ticker")
                continue

            # 3. Full registered name.
            if self._contains_word(lowered, inst.name):
                if inst.ambiguous_name and not has_context:
                    # An ordinary word with no financial framing around it.
                    continue
                matches[symbol] = EntityMatch(
                    symbol, 0.85 if not inst.ambiguous_name else 0.6, "full_name"
                )
                continue

            # 4. Aliases - weakest evidence, so context is required.
            for alias in inst.aliases:
                if self._contains_word(lowered, alias):
                    if inst.ambiguous_name and not has_context:
                        continue
                    matches[symbol] = EntityMatch(symbol, 0.7, "alias")
                    break

        out = sorted(matches.values(), key=lambda m: -m.confidence)

        # A headline naming several companies is usually a market roundup, not
        # a story about any one of them.
        if len(out) > 3:
            log.debug("headline names %d companies - treating as a roundup", len(out))
            return []
        return out

    def primary(self, text: str) -> EntityMatch | None:
        """The single best match, or None when it is genuinely ambiguous."""
        matches = self.resolve(text)
        if not matches:
            return None
        if len(matches) > 1 and matches[0].confidence - matches[1].confidence < 0.1:
            log.debug("ambiguous between %s and %s - dropping",
                      matches[0].symbol, matches[1].symbol)
            return None
        return matches[0]


# A starter universe: large Stockholm listings plus a few US mega caps.
# Extend it with your own watchlist - the resolver is only as good as this.
DEFAULT_UNIVERSE: list[Instrument] = [
    Instrument("VOLV-B.ST", "volvo", ["ab volvo", "volvo group"]),
    Instrument("ERIC-B.ST", "ericsson", ["telefonaktiebolaget lm ericsson"]),
    Instrument("ATCO-A.ST", "atlas copco"),
    Instrument("SEB-A.ST", "seb", ["skandinaviska enskilda banken"]),
    Instrument("SWED-A.ST", "swedbank"),
    Instrument("INVE-B.ST", "investor ab", ["investor"], ambiguous_name=True),
    Instrument("HM-B.ST", "h&m", ["hennes & mauritz", "hennes och mauritz"]),
    Instrument("SAND.ST", "sandvik"),
    Instrument("ASSA-B.ST", "assa abloy"),
    Instrument("EVO.ST", "evolution ab", ["evolution gaming"]),
    Instrument("AAPL", "apple", ["apple inc"], ambiguous_name=True),
    Instrument("MSFT", "microsoft"),
    Instrument("NVDA", "nvidia"),
    Instrument("TSLA", "tesla"),
    Instrument("AMZN", "amazon"),
    Instrument("GOOGL", "alphabet", ["google"]),
]
