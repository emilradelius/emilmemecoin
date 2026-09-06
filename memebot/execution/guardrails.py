"""Circuit breakers for automated trading.

Every one of these exists because of a specific way an automated trader
destroys an account:

* **Daily loss cap** - a bad market day compounds. A bot with no loss cap
  will keep taking the same losing setup until the wallet is empty.
* **Daily trade cap** - a bug in signal generation (a feed replaying old
  data, a stuck loop) shows up as a burst of trades. The cap bounds the
  damage from a class of bug you have not thought of yet.
* **Consecutive-loss halt** - the strategy has stopped working, or the
  market regime changed. Either way, stop and look.
* **Concurrent position cap** - bounds total exposure.
* **Minimum wallet balance** - leaves gas, and leaves a floor.

A tripped breaker halts trading and requires an explicit ``/resume``. It is
deliberately not auto-clearing: the point is that a human looks at it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import Config
from ..store import Store

log = logging.getLogger(__name__)


@dataclass
class BreakerState:
    halted: bool = False
    halt_reason: str = ""
    halted_at: float = 0.0
    day: str = ""
    trades_today: int = 0
    realized_pnl_sol_today: float = 0.0
    consecutive_losses: int = 0
    history: list[str] = field(default_factory=list)


class Guardrails:
    KEY = "breaker_state"

    def __init__(self, cfg: Config, store: Store) -> None:
        e = cfg.section("execution")
        self.max_daily_loss = abs(e.get("max_daily_loss_sol", 2.0))
        self.max_daily_trades = e.get("max_daily_trades", 12)
        self.max_consecutive_losses = e.get("halt_on_consecutive_losses", 4)
        self.max_concurrent = e.get("max_concurrent_positions", 5)
        self.min_balance = e.get("min_wallet_balance_sol", 0.05)
        self.base_size = e.get("base_position_sol", 0.25)
        self.max_size = e.get("max_position_sol", 1.0)
        self.scale_with_conviction = e.get("scale_size_with_conviction", True)
        self.max_price_impact = e.get("max_price_impact_pct", 8.0)
        self.store = store

    # --- state ------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def state(self) -> BreakerState:
        raw = self.store.kv_get(self.KEY) or {}
        st = BreakerState(**{k: v for k, v in raw.items() if k in BreakerState.__annotations__})
        if st.day != self._today():
            # Daily counters reset; a halt does NOT reset with the date. A
            # breaker that clears itself overnight is not a breaker.
            st.day = self._today()
            st.trades_today = 0
            st.realized_pnl_sol_today = 0.0
            self._save(st)
        return st

    def _save(self, st: BreakerState) -> None:
        self.store.kv_set(self.KEY, st.__dict__)

    # --- checks -----------------------------------------------------------
    def can_open(
        self, *, open_positions: int, wallet_balance_sol: float | None = None
    ) -> tuple[bool, str]:
        st = self.state()
        if st.halted:
            return False, f"halted: {st.halt_reason}"
        if st.trades_today >= self.max_daily_trades:
            return False, f"daily trade cap ({self.max_daily_trades})"
        if st.realized_pnl_sol_today <= -self.max_daily_loss:
            self.halt(f"daily loss cap hit ({st.realized_pnl_sol_today:.2f} SOL)")
            return False, "daily loss cap"
        if st.consecutive_losses >= self.max_consecutive_losses:
            self.halt(f"{st.consecutive_losses} consecutive losses")
            return False, "consecutive losses"
        if open_positions >= self.max_concurrent:
            return False, f"max concurrent positions ({self.max_concurrent})"
        if wallet_balance_sol is not None and wallet_balance_sol < self.min_balance:
            return False, (
                f"wallet balance {wallet_balance_sol:.3f} SOL below floor "
                f"{self.min_balance}"
            )
        return True, "ok"

    def position_size(self, conviction: float, *, strong_threshold: float = 5.5) -> float:
        """Scale size with conviction, between base and max.

        Conviction at the alert threshold buys the base size; roughly double
        the threshold buys the maximum. Clamped at both ends so an outlier
        conviction score cannot produce an outlier position.
        """
        if not self.scale_with_conviction:
            return self.base_size
        if strong_threshold <= 0:
            return self.base_size
        excess = max(0.0, conviction - strong_threshold) / strong_threshold
        size = self.base_size + (self.max_size - self.base_size) * min(1.0, excess)
        return round(max(self.base_size, min(self.max_size, size)), 4)

    # --- recording --------------------------------------------------------
    def record_open(self) -> None:
        st = self.state()
        st.trades_today += 1
        self._save(st)

    def record_close(self, pnl_sol: float) -> None:
        st = self.state()
        st.realized_pnl_sol_today = round(st.realized_pnl_sol_today + pnl_sol, 6)
        if pnl_sol < 0:
            st.consecutive_losses += 1
        else:
            st.consecutive_losses = 0
        self._save(st)

        if st.realized_pnl_sol_today <= -self.max_daily_loss:
            self.halt(
                f"daily loss cap: {st.realized_pnl_sol_today:.2f} SOL "
                f"(limit -{self.max_daily_loss})"
            )
        elif st.consecutive_losses >= self.max_consecutive_losses:
            self.halt(f"{st.consecutive_losses} consecutive losing trades")

    def halt(self, reason: str) -> None:
        st = self.state()
        if st.halted:
            return
        st.halted = True
        st.halt_reason = reason
        st.halted_at = time.time()
        st.history = (st.history + [f"{time.strftime('%Y-%m-%d %H:%M')} {reason}"])[-20:]
        self._save(st)
        log.error("TRADING HALTED: %s", reason)

    def resume(self) -> str:
        st = self.state()
        if not st.halted:
            return "Not halted."
        was = st.halt_reason
        st.halted = False
        st.halt_reason = ""
        st.consecutive_losses = 0
        self._save(st)
        log.warning("trading resumed (was halted: %s)", was)
        return f"Resumed. Was halted for: {was}"

    def status(self) -> str:
        st = self.state()
        lines = [
            f"Halted: {'YES - ' + st.halt_reason if st.halted else 'no'}",
            f"Trades today: {st.trades_today}/{self.max_daily_trades}",
            f"PnL today: {st.realized_pnl_sol_today:+.3f} SOL "
            f"(cap -{self.max_daily_loss})",
            f"Consecutive losses: {st.consecutive_losses}/{self.max_consecutive_losses}",
        ]
        return "\n".join(lines)
