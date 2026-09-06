"""Mode switching: alerts / paper / live.

Switching to live is deliberately awkward. There are **two independent
locks**, and both must be open:

1. ``execution.live_mode_armed: true`` in ``config.yaml`` - set on the
   machine, in a file, on purpose.
2. ``TRADING_WALLET_PRIVATE_KEY`` present in the environment.

``/mode live`` from Telegram checks both and refuses if either is missing. It
cannot set them. That asymmetry is the point: your phone can always *stop*
trading instantly, but it can never start it. If someone gets into your
Telegram, the worst they can do is turn the bot off.
"""

from __future__ import annotations

import logging
import os

from ..config import Config
from ..enrich.dexscreener import DexScreener
from ..models import Position
from ..store import Store
from .base import Executor, OrderResult
from .guardrails import Guardrails
from .paper import PaperExecutor

log = logging.getLogger(__name__)

VALID_MODES = ("alerts", "paper", "live")


class NullExecutor(Executor):
    """Alerts-only. Records nothing, places nothing."""

    mode = "alerts"

    async def buy(self, mint: str, size_sol: float, *,
                  price_usd: float | None = None) -> OrderResult:
        return OrderResult(False, "alerts-only mode: no order placed")

    async def sell(self, position: Position, fraction: float, *,
                   price_usd: float | None = None) -> OrderResult:
        return OrderResult(False, "alerts-only mode: no order placed")


class ExecutionManager:
    KEY = "execution_mode"

    def __init__(self, cfg: Config, store: Store, dex: DexScreener) -> None:
        self.cfg = cfg
        self.store = store
        self.dex = dex
        self.guardrails = Guardrails(cfg, store)

        self._configured_mode = cfg.get("execution.mode", "alerts")
        # A mode set at runtime survives restarts; without this, a crash at
        # 3am would silently revert you to whatever the file says.
        self._mode = store.kv_get(self.KEY) or self._configured_mode
        if self._mode not in VALID_MODES:
            self._mode = "alerts"

        # If live was active but the arming lock has since been closed, fall
        # back rather than continuing to trade.
        if self._mode == "live" and not self.live_available()[0]:
            log.warning("stored mode was 'live' but live is not available; using paper")
            self._mode = "paper"
            store.kv_set(self.KEY, self._mode)

        self._executor: Executor = self._build(self._mode)

    # --- availability -----------------------------------------------------
    def live_available(self) -> tuple[bool, str]:
        if not self.cfg.get("execution.live_mode_armed", False):
            return False, (
                "execution.live_mode_armed is false in config.yaml. "
                "This lock can only be opened on the machine, not from here."
            )
        if not os.getenv("TRADING_WALLET_PRIVATE_KEY"):
            return False, "TRADING_WALLET_PRIVATE_KEY is not set in the environment."
        try:
            import solders  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            return False, "solders is not installed (pip install -r requirements-live.txt)"
        return True, "ok"

    def _build(self, mode: str) -> Executor:
        if mode == "live":
            ok, why = self.live_available()
            if not ok:
                log.error("refusing to build live executor: %s", why)
                return PaperExecutor(self.cfg, self.dex)
            from .live import LiveExecutor
            return LiveExecutor(self.cfg, self.dex)
        if mode == "paper":
            return PaperExecutor(self.cfg, self.dex)
        return NullExecutor()

    # --- mode -------------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def executor(self) -> Executor:
        return self._executor

    @property
    def trades(self) -> bool:
        return self._mode in ("paper", "live")

    async def set_mode(self, mode: str) -> str:
        mode = mode.strip().lower()
        if mode not in VALID_MODES:
            return f"Unknown mode '{mode}'. Valid: {', '.join(VALID_MODES)}"
        if mode == self._mode:
            return f"Already in {mode} mode."

        if mode == "live":
            ok, why = self.live_available()
            if not ok:
                return (
                    f"❌ Cannot switch to live: {why}\n\n"
                    "This is intentional. Live trading can only be enabled on "
                    "the machine, never from Telegram."
                )

        old = self._executor
        self._mode = mode
        self.store.kv_set(self.KEY, mode)
        self._executor = self._build(mode)
        if old is not self._executor:
            await old.close()

        log.warning("execution mode changed to %s", mode)
        if mode == "live":
            return (
                "⚡️ <b>LIVE TRADING ENABLED</b>\n\n"
                "Real orders will be placed with real money.\n"
                f"Position size {self.guardrails.base_size}-"
                f"{self.guardrails.max_size} SOL, "
                f"daily loss cap {self.guardrails.max_daily_loss} SOL, "
                f"max {self.guardrails.max_concurrent} concurrent.\n\n"
                "/panic stops everything instantly."
            )
        if mode == "paper":
            return "📝 Paper mode. Simulated fills with slippage and fees, no real money."
        return "🔔 Alerts-only. No orders will be placed."

    async def panic(self) -> str:
        """Kill switch: halt trading and drop to alerts-only immediately."""
        self.guardrails.halt("PANIC - manual kill switch")
        was = self._mode
        self._mode = "alerts"
        self.store.kv_set(self.KEY, "alerts")
        old, self._executor = self._executor, NullExecutor()
        await old.close()
        log.error("PANIC triggered from %s mode", was)
        return (
            f"🛑 <b>PANIC</b>\n\nWas: {was}. Now: alerts-only, trading halted.\n"
            "Open positions were NOT closed - close them yourself.\n"
            "Use /resume then /mode to restart."
        )

    def status(self) -> str:
        ok, why = self.live_available()
        return (
            f"<b>Mode</b> {self._mode}\n"
            f"<b>Live available</b> {'yes' if ok else 'no - ' + why}\n\n"
            f"{self.guardrails.status()}"
        )

    async def close(self) -> None:
        await self._executor.close()
