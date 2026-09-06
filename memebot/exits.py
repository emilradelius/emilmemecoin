"""Exit monitoring.

A bot that only tells you when to buy is worse than useless - it gives you
entries with no plan, which is how a 3x becomes a round trip to zero. Every
position opened from an alert is tracked until it is closed.

Six exit triggers, checked in priority order:

1. **Safety regression / liquidity collapse** - the LP is being pulled or an
   authority came back. Emitted immediately as URGENT; nothing else matters.
2. **Smart-money exit** - the traders whose buying triggered the alert are
   selling. This is the highest-quality *informational* exit available,
   because it is the same signal that got you in, running backwards.
3. **Stop loss** - mechanical, from the alert price.
4. **Take-profit ladder** - partial exits on the way up, so a position that
   round-trips still banked something.
5. **Trailing stop** - activates after the first ladder rung, to let a runner
   run without giving all of it back.
6. **Time stop** - a meme coin that has gone nowhere in a day is dead money.

Exit alerts deliberately bypass the daily alert budget and quiet hours.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import Config
from .enrich.dexscreener import DexScreener
from .models import (
    Alert, ExitReason, Position, Side, Signal, Source, TokenSafety,
)
from .scoring.safety import SafetyGate
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    fraction: float = 1.0
    detail: str = ""
    urgent: bool = False


class ExitMonitor:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        dex: DexScreener,
        safety: SafetyGate,
    ) -> None:
        e = cfg.section("exits")
        self.enabled = e.get("enabled", True)
        self.poll_interval = e.get("poll_interval_seconds", 30)
        self.smart_exit_pct = e.get("smart_money_exit_pct", 0.40)
        self.stop_loss_pct = e.get("stop_loss_pct", 0.45)
        self.ladder = sorted(
            e.get("take_profit_ladder", []) or [],
            key=lambda r: r.get("multiple", 0),
        )
        self.trailing_stop_pct = e.get("trailing_stop_pct", 0.35)
        self.emergency_liq_drop = e.get("emergency_on_liquidity_drop_pct", 0.50)
        self.emergency_on_safety = e.get("emergency_on_safety_regression", True)
        self.max_hold_hours = e.get("max_hold_hours", 24)

        self.store = store
        self.dex = dex
        self.safety = safety
        # Liquidity at entry, per position, to detect collapse.
        self._entry_liquidity: dict[str, float] = {}

    def remember_entry_liquidity(self, position_id: str, liquidity_usd: float | None) -> None:
        if liquidity_usd:
            self._entry_liquidity[position_id] = liquidity_usd

    # --- individual checks -------------------------------------------------
    def check_safety_regression(
        self, pos: Position, current_liq: float | None, safety: TokenSafety | None
    ) -> ExitDecision | None:
        entry_liq = self._entry_liquidity.get(pos.id)
        if entry_liq and current_liq is not None:
            drop = 1.0 - (current_liq / entry_liq)
            if drop >= self.emergency_liq_drop:
                return ExitDecision(
                    True, ExitReason.LIQUIDITY_COLLAPSE, 1.0,
                    f"Liquidity fell {drop:.0%} since entry "
                    f"(${entry_liq:,.0f} -> ${current_liq:,.0f}). "
                    f"This is what a rug looks like in progress.",
                    urgent=True,
                )
        if self.emergency_on_safety and safety and not safety.passed:
            hard = [
                f for f in safety.failures
                if any(k in f for k in ("authority", "lp_", "rugcheck_danger"))
            ]
            if hard:
                return ExitDecision(
                    True, ExitReason.SAFETY_REGRESSION, 1.0,
                    f"Token safety regressed: {', '.join(hard[:3])}",
                    urgent=True,
                )
        return None

    def check_smart_money(self, pos: Position, recent: list[Signal]) -> ExitDecision | None:
        """Are the traders who got us in now leaving?"""
        if not pos.trigger_actors:
            return None
        triggers = set(pos.trigger_actors)
        sellers = {
            s.actor_id for s in recent
            if s.side is Side.SELL
            and s.actor_id in triggers
            and s.source is Source.PUMPFUN
        }
        if not sellers:
            return None
        fraction_leaving = len(sellers) / len(triggers)
        if fraction_leaving >= self.smart_exit_pct:
            return ExitDecision(
                True, ExitReason.SMART_MONEY_EXIT, 1.0,
                f"{len(sellers)} of the {len(triggers)} traders whose buying "
                f"triggered this alert are now selling.",
            )
        return None

    def check_stop_loss(self, pos: Position, price: float) -> ExitDecision | None:
        if pos.entry_price_usd <= 0:
            return None
        drawdown = 1.0 - (price / pos.entry_price_usd)
        if drawdown >= self.stop_loss_pct:
            return ExitDecision(
                True, ExitReason.STOP_LOSS, 1.0,
                f"Down {drawdown:.0%} from entry (stop is {self.stop_loss_pct:.0%}).",
            )
        return None

    def check_take_profit(self, pos: Position, price: float) -> ExitDecision | None:
        mult = pos.multiple(price)
        for rung in self.ladder:
            target = rung.get("multiple", 0)
            if mult >= target and target not in pos.ladder_rungs_hit:
                return ExitDecision(
                    True, ExitReason.TAKE_PROFIT, rung.get("sell_pct", 0.33),
                    f"Hit {target:.0f}x. Taking {rung.get('sell_pct', 0.33):.0%} "
                    f"off the table, letting the rest run.",
                )
        return None

    def check_trailing_stop(self, pos: Position, price: float) -> ExitDecision | None:
        # Only trails once the first profit rung has been banked; before that
        # the plain stop loss governs, otherwise normal early volatility would
        # shake you out of every position.
        if not pos.ladder_rungs_hit or pos.peak_price_usd <= 0:
            return None
        drop_from_peak = 1.0 - (price / pos.peak_price_usd)
        if drop_from_peak >= self.trailing_stop_pct:
            return ExitDecision(
                True, ExitReason.TRAILING_STOP, 1.0,
                f"Down {drop_from_peak:.0%} from the "
                f"{pos.multiple(pos.peak_price_usd):.1f}x peak.",
            )
        return None

    def check_time_stop(self, pos: Position, price: float,
                        *, now: float | None = None) -> ExitDecision | None:
        if pos.age_hours(now) >= self.max_hold_hours:
            return ExitDecision(
                True, ExitReason.TIME_STOP, 1.0,
                f"Held {pos.age_hours(now):.0f}h at {pos.multiple(price):.2f}x. "
                f"Capital is better used elsewhere.",
            )
        return None

    # --- orchestration -----------------------------------------------------
    async def evaluate(
        self, pos: Position, *, recent_signals: list[Signal] | None = None,
        now: float | None = None,
    ) -> tuple[ExitDecision, float | None]:
        """Run all checks in priority order. Returns the decision and the
        current price (so the caller does not have to fetch it again)."""
        market = await self.dex.get(pos.token_mint)
        price = market.price_usd if market else None
        liq = market.liquidity_usd if market else None

        if price is None:
            # No price usually means no pool - which is itself the alarm.
            return (
                ExitDecision(
                    True, ExitReason.LIQUIDITY_COLLAPSE, 1.0,
                    "No market data - the pool may have been removed.",
                    urgent=True,
                ),
                None,
            )

        if price > pos.peak_price_usd:
            pos.peak_price_usd = price

        safety = None
        if liq is not None and self.emergency_on_safety:
            entry_liq = self._entry_liquidity.get(pos.id)
            # Only pay for a full safety re-check when something looks wrong.
            if entry_liq and liq < entry_liq * 0.7:
                safety = await self.safety.check(pos.token_mint)

        for check in (
            lambda: self.check_safety_regression(pos, liq, safety),
            lambda: self.check_smart_money(pos, recent_signals or []),
            lambda: self.check_stop_loss(pos, price),
            lambda: self.check_take_profit(pos, price),
            lambda: self.check_trailing_stop(pos, price),
            lambda: self.check_time_stop(pos, price, now=now),
        ):
            decision = check()
            if decision and decision.should_exit:
                return decision, price

        return ExitDecision(False), price

    def apply_partial(self, pos: Position, decision: ExitDecision, price: float) -> None:
        """Record a partial exit against the position."""
        if decision.reason is ExitReason.TAKE_PROFIT:
            for rung in self.ladder:
                target = rung.get("multiple", 0)
                if pos.multiple(price) >= target and target not in pos.ladder_rungs_hit:
                    pos.ladder_rungs_hit.append(target)
                    break
        pos.remaining_fraction = max(0.0, pos.remaining_fraction - decision.fraction)
        if pos.remaining_fraction <= 0.01:
            pos.closed_at = time.time()
            pos.close_reason = decision.reason
        self.store.save_position(pos)

    def build_alert(self, pos: Position, decision: ExitDecision, price: float) -> Alert:
        return Alert(
            kind="exit",
            token_mint=pos.token_mint,
            token_symbol=pos.token_symbol,
            text="",
            position=pos,
            exit_reason=decision.reason,
            urgent=decision.urgent,
        )
