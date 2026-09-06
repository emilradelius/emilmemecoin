"""RugCheck token risk data.

RugCheck exposes a public REST API at ``api.rugcheck.xyz``. It answers the
questions that decide whether a token is an asset or a trap: can the deployer
mint more supply, can they freeze your wallet, is the liquidity pool actually
locked, and how concentrated is the holder base.

Their response schema has shifted over time and differs slightly between the
full report and the summary endpoint, so every field is read defensively -
a missing field becomes ``None`` and the safety gate treats unknown as unsafe
rather than assuming the best.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..http import HttpClient

log = logging.getLogger(__name__)

BASE = "https://api.rugcheck.xyz/v1"

# Risk names RugCheck uses that we always treat as disqualifying, regardless
# of the numeric score attached to them.
CRITICAL_RISKS = {
    "mint authority still enabled",
    "freeze authority still enabled",
    "large amount of lp unlocked",
    "single holder ownership",
    "copycat token",
}


@dataclass(slots=True)
class RiskReport:
    mint: str
    score: float | None = None
    """RugCheck's composite. Note their scale is INVERTED: lower is safer."""

    score_normalised: float | None = None
    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    lp_locked_pct: float | None = None
    lp_burned: bool | None = None
    total_holders: int | None = None
    top10_pct: float | None = None
    top_holder_pct: float | None = None
    creator_pct: float | None = None
    insider_pct: float | None = None
    total_liquidity_usd: float | None = None
    danger_flags: list[str] = field(default_factory=list)
    warn_flags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    fetched: bool = False
    """False means the lookup failed. Distinguishes 'clean' from 'unknown'."""

    @property
    def has_danger(self) -> bool:
        return bool(self.danger_flags)


def _b(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.strip() == "" or v.lower() in {"null", "none"}:
            return None
        return True
    return bool(v)


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class RugCheck:
    def __init__(self, client: HttpClient | None = None, api_key: str | None = None) -> None:
        headers = {"X-API-KEY": api_key} if api_key else None
        # Token risk barely changes minute to minute, so a 2-minute cache is
        # safe and keeps us well inside their limits.
        self.http = client or HttpClient(rate=2.0, cache_ttl=120.0, headers=headers)

    async def close(self) -> None:
        await self.http.close()

    async def report(self, mint: str) -> RiskReport:
        data = await self.http.get_json(f"{BASE}/tokens/{mint}/report")
        if not data or not isinstance(data, dict):
            log.debug("rugcheck lookup failed for %s", mint)
            return RiskReport(mint=mint, fetched=False)
        return self._parse(mint, data)

    def _parse(self, mint: str, d: dict[str, Any]) -> RiskReport:
        rep = RiskReport(mint=mint, fetched=True, raw=d)

        rep.score = _f(d.get("score"))
        rep.score_normalised = _f(d.get("score_normalised") or d.get("scoreNormalised"))

        token = d.get("token") or {}
        # RugCheck returns the authority address, or null/"" when revoked.
        mint_auth = token.get("mintAuthority", d.get("mintAuthority"))
        freeze_auth = token.get("freezeAuthority", d.get("freezeAuthority"))
        if "mintAuthority" in token or "mintAuthority" in d:
            rep.mint_authority_revoked = _b(mint_auth) is not True
        if "freezeAuthority" in token or "freezeAuthority" in d:
            rep.freeze_authority_revoked = _b(freeze_auth) is not True

        rep.total_holders = int(d["totalHolders"]) if d.get("totalHolders") is not None else None
        rep.total_liquidity_usd = _f(d.get("totalMarketLiquidity"))

        # --- holder concentration ---------------------------------------
        holders = d.get("topHolders") or []
        pcts: list[float] = []
        insider_total = 0.0
        for h in holders:
            pct = _f(h.get("pct"))
            if pct is None:
                continue
            # Skip the pool/bonding-curve account itself: it "holds" most of
            # the supply by design and counting it would reject everything.
            if h.get("insider") is True:
                insider_total += pct
            owner_label = (h.get("owner") or "").lower()
            if h.get("isLiquidityPool") or "liquidity" in owner_label or "amm" in owner_label:
                continue
            pcts.append(pct)
        if pcts:
            pcts.sort(reverse=True)
            rep.top10_pct = round(sum(pcts[:10]), 4)
            rep.top_holder_pct = round(pcts[0], 4)
        if insider_total:
            rep.insider_pct = round(insider_total, 4)

        creator_bal = d.get("creatorBalance")
        supply = _f(token.get("supply"))
        if creator_bal is not None and supply:
            decimals = token.get("decimals") or 0
            try:
                rep.creator_pct = round(
                    float(creator_bal) / (supply / (10 ** 0 if decimals is None else 1)) * 100.0, 4
                )
            except (TypeError, ValueError, ZeroDivisionError):
                rep.creator_pct = None

        # --- liquidity pool locking -------------------------------------
        markets = d.get("markets") or []
        lp_pcts: list[float] = []
        burned = False
        for m in markets:
            lp = m.get("lp") or {}
            locked_pct = _f(lp.get("lpLockedPct"))
            if locked_pct is not None:
                lp_pcts.append(locked_pct)
            # A burned LP shows up as locked 100% or as the burn address
            # holding the LP tokens.
            if _f(lp.get("lpLockedPct")) == 100.0 or lp.get("lpBurned"):
                burned = True
        if lp_pcts:
            rep.lp_locked_pct = max(lp_pcts)
            rep.lp_burned = burned or rep.lp_locked_pct >= 99.0

        # --- named risks -------------------------------------------------
        for risk in d.get("risks") or []:
            name = (risk.get("name") or "").strip()
            level = (risk.get("level") or "").lower()
            if not name:
                continue
            if level == "danger" or name.lower() in CRITICAL_RISKS:
                rep.danger_flags.append(name)
            elif level in {"warn", "warning"}:
                rep.warn_flags.append(name)

        return rep
