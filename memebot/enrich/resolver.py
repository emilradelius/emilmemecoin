"""Turn free text into canonical Solana mint addresses.

This module exists because of one hard fact: **ticker symbols are not
identifiers**. At any moment there are dozens of live Solana tokens called
``$MOON``, and a scammer can deploy a token with your target's exact ticker
in seconds specifically to catch bots that match on symbols. Acting on a
ticker without resolving it is the single easiest way to buy the wrong thing.

So the resolution ladder is, in order of trust:

1. **An explicit mint address in the text** - unambiguous, confidence 1.0.
2. **A mint inside a known URL** (pump.fun, dexscreener, birdeye, solscan,
   axiom, photon, gmgn) - unambiguous, confidence 0.95.
3. **A bare ``$TICKER``** - ambiguous. Searched against DexScreener, filtered
   for plausibility, and returned with a confidence well below 1.0 that
   scales with how dominant the best candidate is. A ticker that resolves to
   several plausible tokens is deliberately given a low confidence so that
   the consensus engine will refuse to act on it alone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .dexscreener import DexScreener, MarketData

log = logging.getLogger(__name__)

# Base58 excludes 0, O, I and l. Solana mints are 32-44 chars.
_B58 = r"[1-9A-HJ-NP-Za-km-z]"
MINT_RE = re.compile(rf"\b({_B58}{{32,44}})\b")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{1,14})\b")

URL_MINT_RE = re.compile(
    rf"(?:pump\.fun/(?:coin/)?|dexscreener\.com/solana/|birdeye\.so/token/"
    rf"|solscan\.io/token/|axiom\.trade/(?:t/|meme/)?|photon-sol\.tinyastro\.io/en/lp/"
    rf"|gmgn\.ai/sol/token/|jup\.ag/swap/[^/\s]+-)({_B58}{{32,44}})"
)

# Things that look like mints but are not tradeable meme coins. Matching one
# of these is almost always a false positive from a transaction signature or
# a wrapped-SOL reference in the text.
DENYLIST = {
    "So11111111111111111111111111111111111111112",   # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",    # SPL token program
    "11111111111111111111111111111111",               # system program
    "ComputeBudget111111111111111111111111111111",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
}

# Common English words that survive the $TICKER regex and are never coins.
TICKER_STOPWORDS = {
    "USD", "SOL", "BTC", "ETH", "USDC", "USDT", "K", "M", "B",
}


@dataclass(slots=True)
class Resolution:
    mint: str
    symbol: str | None
    confidence: float
    method: str
    """One of: ``explicit_mint``, ``url``, ``ticker_search``."""
    market: MarketData | None = None
    alternatives: int = 0
    """How many other plausible tokens shared this ticker. >0 means the text
    was genuinely ambiguous."""


def looks_like_mint(text: str) -> bool:
    return bool(MINT_RE.fullmatch(text.strip()))


def extract_mints(text: str) -> list[str]:
    """All plausible mint addresses in the text, URLs first.

    pump.fun mints conventionally end in ``pump``; when several base58 blobs
    are present that suffix is a strong tiebreaker, so those are surfaced
    ahead of the rest.
    """
    found: list[str] = []
    for m in URL_MINT_RE.finditer(text):
        if m.group(1) not in DENYLIST:
            found.append(m.group(1))
    bare = [m.group(1) for m in MINT_RE.finditer(text) if m.group(1) not in DENYLIST]
    bare.sort(key=lambda a: (not a.endswith("pump"), len(a)))
    for b in bare:
        if b not in found:
            found.append(b)
    return found


def extract_tickers(text: str) -> list[str]:
    out: list[str] = []
    for m in TICKER_RE.finditer(text):
        t = m.group(1).upper()
        if t in TICKER_STOPWORDS or t in out:
            continue
        out.append(t)
    return out


class TokenResolver:
    def __init__(
        self,
        dex: DexScreener,
        *,
        min_liquidity_usd: float = 10_000,
        max_age_hours: float = 72,
        ticker_base_confidence: float = 0.6,
    ) -> None:
        self.dex = dex
        self.min_liquidity_usd = min_liquidity_usd
        self.max_age_hours = max_age_hours
        self.ticker_base_confidence = ticker_base_confidence

    async def resolve(self, text: str) -> list[Resolution]:
        """Resolve every token reference in a piece of text.

        Explicit mints short-circuit ticker resolution: if someone posts both
        a CA and a ticker, the CA is what they meant, and searching the ticker
        would only add a chance of picking up an impostor.
        """
        resolutions: list[Resolution] = []
        url_mints = {m.group(1) for m in URL_MINT_RE.finditer(text)}

        mints = extract_mints(text)
        for mint in mints:
            market = await self.dex.get(mint)
            # A base58 string that no DEX has ever heard of is a transaction
            # signature or a wallet, not a token. Drop it silently.
            if market is None:
                log.debug("no market for candidate mint %s - discarding", mint)
                continue
            resolutions.append(
                Resolution(
                    mint=mint,
                    symbol=market.symbol,
                    confidence=0.95 if mint in url_mints else 1.0,
                    method="url" if mint in url_mints else "explicit_mint",
                    market=market,
                )
            )

        if resolutions:
            return resolutions

        for ticker in extract_tickers(text):
            res = await self.resolve_ticker(ticker)
            if res:
                resolutions.append(res)
        return resolutions

    async def resolve_ticker(self, ticker: str) -> Resolution | None:
        """Best-effort ticker resolution, with an honest confidence score."""
        candidates = await self.dex.search(f"${ticker}")
        if not candidates:
            candidates = await self.dex.search(ticker)
        if not candidates:
            return None

        plausible = [
            c
            for c in candidates
            if (c.symbol or "").upper() == ticker.upper()
            and (c.liquidity_usd or 0) >= self.min_liquidity_usd
            and (c.age_minutes is None or c.age_minutes <= self.max_age_hours * 60)
        ]
        if not plausible:
            return None

        best = plausible[0]
        confidence = self.ticker_base_confidence

        # Dominance: if the top candidate holds most of the liquidity across
        # everything sharing this ticker, the reference is probably to it.
        # If two tokens are neck and neck, we genuinely cannot tell, and the
        # confidence must say so.
        total_liq = sum(c.liquidity_usd or 0 for c in plausible)
        if total_liq > 0:
            dominance = (best.liquidity_usd or 0) / total_liq
            if dominance >= 0.9:
                confidence = min(0.85, confidence + 0.25)
            elif dominance >= 0.7:
                confidence = min(0.75, confidence + 0.10)
            else:
                # Genuinely contested ticker - halve it. The consensus engine
                # will need corroboration from another source to act.
                confidence *= 0.5

        return Resolution(
            mint=best.mint,
            symbol=best.symbol,
            confidence=round(confidence, 3),
            method="ticker_search",
            market=best,
            alternatives=len(plausible) - 1,
        )
