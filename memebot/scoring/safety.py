"""The hard safety gate.

This is the highest-value component in the bot. In meme coins the dominant
loss mode is not bad timing, it is buying something that was engineered to
take your money: the deployer mints more supply, freezes your wallet, or pulls
the liquidity pool. Those are all detectable *before* you buy.

Design rule: **unknown is unsafe.** If an upstream lookup fails and we cannot
confirm that the mint authority is revoked, the token is rejected. Failing
closed costs you missed opportunities; failing open costs you the position.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..enrich.dexscreener import DexScreener, MarketData
from ..enrich.rugcheck import RiskReport, RugCheck
from ..models import TokenSafety

log = logging.getLogger(__name__)


class SafetyGate:
    def __init__(self, cfg: Config, dex: DexScreener, rug: RugCheck) -> None:
        self.cfg = cfg
        self.dex = dex
        self.rug = rug
        s = cfg.section("safety")
        self.enabled = s.get("enabled", True)
        self.min_liquidity = s.get("min_liquidity_usd", 15000)
        self.max_liquidity = s.get("max_liquidity_usd", 0)
        self.min_age_minutes = s.get("min_age_minutes", 5)
        self.max_age_hours = s.get("max_age_hours", 72)
        self.req_mint_revoked = s.get("require_mint_authority_revoked", True)
        self.req_freeze_revoked = s.get("require_freeze_authority_revoked", True)
        self.req_lp_locked = s.get("require_lp_burned_or_locked", True)
        self.max_top10 = s.get("max_top10_holder_pct", 30.0)
        self.max_single = s.get("max_single_holder_pct", 10.0)
        self.max_dev = s.get("max_dev_holding_pct", 5.0)
        self.min_holders = s.get("min_unique_holders", 75)
        self.max_bundled = s.get("max_bundled_supply_pct", 25.0)
        self.max_vol_liq = s.get("max_volume_to_liquidity_ratio", 50.0)
        self.min_1h_volume = s.get("min_1h_volume_usd", 5000)
        self.min_1h_buyers = s.get("min_1h_unique_buyers", 30)
        self.max_rugcheck_score = s.get("max_rugcheck_score", 2500)
        self.reject_danger = s.get("reject_rugcheck_danger_flags", True)

    async def check(self, mint: str) -> TokenSafety:
        result = TokenSafety(mint=mint, passed=True)
        if not self.enabled:
            result.warn("safety_gate_disabled")
            return result

        # Non-Solana assets (AlphaLedger majors) do not have mints and are not
        # things this gate can reason about. They never trigger buys on their
        # own - they only act as corroboration - so pass them through marked.
        if mint.startswith("cex:"):
            result.warn("non_solana_asset_not_gated")
            return result

        market = await self.dex.get(mint)
        report = await self.rug.report(mint)
        self._apply_market(result, market)
        self._apply_risk(result, report)
        return result

    # --- market-data checks ----------------------------------------------
    def _apply_market(self, r: TokenSafety, m: MarketData | None) -> None:
        if m is None:
            r.fail("no_market_data")
            return

        r.liquidity_usd = m.liquidity_usd
        r.volume_24h_usd = m.volume_24h_usd
        r.volume_1h_usd = m.volume_1h_usd
        r.price_usd = m.price_usd
        r.age_minutes = m.age_minutes

        if m.liquidity_usd is None:
            r.fail("unknown_liquidity")
        elif m.liquidity_usd < self.min_liquidity:
            r.fail(f"liquidity_too_low(${m.liquidity_usd:,.0f}<${self.min_liquidity:,.0f})")
        elif self.max_liquidity and m.liquidity_usd > self.max_liquidity:
            r.fail(f"liquidity_too_high(${m.liquidity_usd:,.0f})")

        if m.age_minutes is None:
            r.warn("unknown_age")
        else:
            if m.age_minutes < self.min_age_minutes:
                r.fail(f"too_new({m.age_minutes:.1f}m<{self.min_age_minutes}m)")
            if m.age_minutes > self.max_age_hours * 60:
                r.fail(f"too_old({m.age_minutes / 60:.1f}h>{self.max_age_hours}h)")

        # Wash trading: real tokens turn over a few times their pool size a
        # day. Fifty times means bots cycling the same SOL to fake volume.
        vl = m.volume_to_liquidity
        if vl is not None and vl > self.max_vol_liq:
            r.fail(f"wash_trading_suspected(vol/liq={vl:.0f})")

        if m.volume_1h_usd is not None and m.volume_1h_usd < self.min_1h_volume:
            r.fail(f"insufficient_1h_volume(${m.volume_1h_usd:,.0f})")

        # Unique buyers are not exposed directly; buy-transaction count in the
        # last hour is the closest available proxy.
        if m.buys_1h < self.min_1h_buyers:
            r.fail(f"too_few_buyers_1h({m.buys_1h}<{self.min_1h_buyers})")

        # A pool where sells overwhelm buys is already distributing.
        if m.buys_1h and m.sells_1h > m.buys_1h * 2:
            r.warn(f"sell_pressure({m.sells_1h}s/{m.buys_1h}b)")

    # --- risk-report checks ----------------------------------------------
    def _apply_risk(self, r: TokenSafety, rep: RiskReport) -> None:
        if not rep.fetched:
            # Unknown is unsafe. We cannot confirm the deployer gave up
            # control, so we do not buy.
            r.fail("rugcheck_unavailable")
            return

        r.rugcheck_score = rep.score
        r.holders = rep.total_holders
        r.top10_pct = rep.top10_pct
        r.dev_pct = rep.creator_pct

        if self.reject_danger and rep.has_danger:
            r.fail(f"rugcheck_danger({', '.join(rep.danger_flags[:3])})")

        if (
            self.max_rugcheck_score
            and rep.score is not None
            and rep.score > self.max_rugcheck_score
        ):
            r.fail(f"rugcheck_score({rep.score:.0f}>{self.max_rugcheck_score})")

        if self.req_mint_revoked and rep.mint_authority_revoked is not True:
            r.fail("mint_authority_active")
        if self.req_freeze_revoked and rep.freeze_authority_revoked is not True:
            r.fail("freeze_authority_active")

        if self.req_lp_locked:
            if rep.lp_burned is True or (rep.lp_locked_pct or 0) >= 99.0:
                pass
            elif rep.lp_locked_pct is None:
                r.fail("lp_lock_unknown")
            else:
                r.fail(f"lp_unlocked({rep.lp_locked_pct:.0f}%_locked)")

        if rep.total_holders is None:
            r.warn("unknown_holder_count")
        elif rep.total_holders < self.min_holders:
            r.fail(f"too_few_holders({rep.total_holders}<{self.min_holders})")

        if rep.top10_pct is not None and rep.top10_pct > self.max_top10:
            r.fail(f"top10_concentration({rep.top10_pct:.1f}%>{self.max_top10}%)")
        if rep.top_holder_pct is not None and rep.top_holder_pct > self.max_single:
            r.fail(f"whale_holder({rep.top_holder_pct:.1f}%>{self.max_single}%)")
        if rep.creator_pct is not None and rep.creator_pct > self.max_dev:
            r.fail(f"dev_holds({rep.creator_pct:.1f}%>{self.max_dev}%)")

        # Insider supply is RugCheck's own label for coordinated/bundled
        # wallets at launch - engineered exit liquidity.
        if rep.insider_pct is not None and rep.insider_pct > self.max_bundled:
            r.fail(f"bundled_launch(insiders_hold_{rep.insider_pct:.1f}%)")

        for flag in rep.warn_flags[:5]:
            r.warn(f"rugcheck:{flag}")
