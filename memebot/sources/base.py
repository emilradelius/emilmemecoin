"""Common interface for signal sources."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from ..models import Signal

log = logging.getLogger(__name__)


class SignalSource(ABC):
    """A long-running producer that pushes :class:`Signal` objects onto a queue.

    Sources must never raise out of :meth:`run`. A source that dies takes its
    coverage with it silently, which is the worst failure mode this system
    has - you would keep receiving alerts, just worse ones, with no
    indication anything was wrong. :meth:`run_forever` enforces that by
    restarting with backoff and reporting sustained failures.
    """

    name: str = "source"

    def __init__(self) -> None:
        self.enabled = True
        self._consecutive_failures = 0
        self.signals_emitted = 0
        self.last_signal_at: float | None = None

    @abstractmethod
    async def run(self, out: asyncio.Queue[Signal]) -> None: ...

    async def close(self) -> None:
        return None

    async def emit(self, out: asyncio.Queue[Signal], sig: Signal) -> None:
        self.signals_emitted += 1
        self.last_signal_at = sig.ts
        await out.put(sig)

    async def run_forever(
        self, out: asyncio.Queue[Signal], on_failure=None
    ) -> None:
        backoff = 2.0
        while True:
            try:
                await self.run(out)
                # A clean return means the source decided to stop (disabled,
                # budget exhausted). Do not hot-loop on it.
                log.info("source %s returned cleanly; stopping", self.name)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                self._consecutive_failures += 1
                log.exception(
                    "source %s crashed (failure #%d); restarting in %.0fs",
                    self.name, self._consecutive_failures, backoff,
                )
                if self._consecutive_failures in (3, 10) and on_failure:
                    try:
                        await on_failure(self.name, self._consecutive_failures)
                    except Exception:
                        log.exception("failure notifier itself failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)
            else:
                self._consecutive_failures = 0
