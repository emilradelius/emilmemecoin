"""Deciding what actually reaches your phone.

Signal quality is only half the problem. A bot that sends thirty alerts a day
gets muted within a week, and a muted bot is worth nothing. This enforces:

* a **daily budget** on buy alerts (default 5),
* **deduplication** so one token cannot alert repeatedly,
* **quiet hours**, with the best suppressed alert delivered as a digest when
  they end,
* and a **tier filter** so only STRONG reaches you by default.

Exit alerts bypass all of it. If the bot told you to buy something, it owes
you the sell regardless of the hour or the budget.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

from ..config import Config
from ..models import Candidate, Tier
from ..store import Store

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


@dataclass
class GateDecision:
    send: bool
    reason: str
    defer: bool = False
    """True when the alert is being held for the end of quiet hours rather
    than dropped."""


class AlertGate:
    def __init__(self, cfg: Config, store: Store) -> None:
        a = cfg.section("alerts")
        self.daily_budget = a.get("daily_budget", 5)
        self.dedupe_hours = a.get("dedupe_hours", 24)
        self.suppress_watch = a.get("suppress_watch_tier", True)
        self.fill_with_watch = a.get("fill_budget_with_watch", False)
        self.tz_name = a.get("timezone", "UTC")
        quiet = a.get("quiet_hours") or {}
        self.quiet_start = self._parse_time(quiet.get("start"))
        self.quiet_end = self._parse_time(quiet.get("end"))
        self.store = store
        self._deferred: list[Candidate] = []

    @staticmethod
    def _parse_time(val: str | None) -> dtime | None:
        if not val:
            return None
        try:
            hh, mm = str(val).split(":")
            return dtime(int(hh), int(mm))
        except (ValueError, TypeError):
            log.warning("could not parse quiet-hours time %r; ignoring", val)
            return None

    def _tz(self):
        if ZoneInfo is None:
            return None
        try:
            return ZoneInfo(self.tz_name)
        except Exception:
            log.warning("unknown timezone %r; quiet hours will use UTC", self.tz_name)
            return None

    def _local_now(self, at: float | None = None) -> datetime:
        ts = at if at is not None else time.time()
        tz = self._tz()
        return datetime.fromtimestamp(ts, tz) if tz else datetime.utcfromtimestamp(ts)

    def in_quiet_hours(self, at: float | None = None) -> bool:
        if not self.quiet_start or not self.quiet_end:
            return False
        now = self._local_now(at).time()
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= now < self.quiet_end
        # Window wraps midnight (e.g. 01:00 -> 07:00 is not wrapped, but
        # 23:00 -> 07:00 is).
        return now >= self.quiet_start or now < self.quiet_end

    def _day_start(self, at: float | None = None) -> float:
        local = self._local_now(at)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()

    def alerts_today(self, at: float | None = None) -> int:
        return self.store.alerts_since(self._day_start(at), kind="buy")

    def budget_remaining(self, at: float | None = None) -> int:
        return max(0, self.daily_budget - self.alerts_today(at))

    # --- the decision -----------------------------------------------------
    def evaluate(self, cand: Candidate, *, at: float | None = None) -> GateDecision:
        at = at if at is not None else time.time()

        if cand.tier is Tier.NONE:
            return GateDecision(False, "below_alert_tier")

        if cand.tier is Tier.WATCH and self.suppress_watch:
            if not (self.fill_with_watch and self.budget_remaining(at) > 0):
                return GateDecision(False, "watch_tier_suppressed")

        # Dedupe: one alert per token per window, unless the tier has been
        # upgraded since (watch -> strong is new information worth sending).
        last = self.store.last_alert_for_mint(cand.token_mint, "buy")
        if last is not None:
            age_h = (at - float(last["ts"])) / 3600.0
            if age_h < self.dedupe_hours:
                upgraded = (
                    last["tier"] == Tier.WATCH.value and cand.tier is Tier.STRONG
                )
                if not upgraded:
                    return GateDecision(False, f"deduped(last_alert_{age_h:.1f}h_ago)")

        if self.budget_remaining(at) <= 0:
            return GateDecision(False, f"daily_budget_exhausted({self.daily_budget})")

        if self.in_quiet_hours(at):
            return GateDecision(False, "quiet_hours", defer=True)

        return GateDecision(True, "ok")

    # --- deferral ---------------------------------------------------------
    def defer(self, cand: Candidate) -> None:
        """Hold a quiet-hours alert. Only the best few survive to morning -
        a queue of twenty stale alerts at 07:00 is not useful."""
        self._deferred.append(cand)
        self._deferred.sort(key=lambda c: c.conviction, reverse=True)
        self._deferred = self._deferred[: self.daily_budget]

    def take_deferred(self, *, max_age_hours: float = 8.0,
                      at: float | None = None) -> list[Candidate]:
        """Drain held alerts once quiet hours end, dropping stale ones.

        A meme coin signal from six hours ago is usually worthless, so these
        are delivered explicitly marked as overnight context rather than as
        live calls.
        """
        at = at if at is not None else time.time()
        fresh = [
            c for c in self._deferred
            if (at - c.computed_at) / 3600.0 <= max_age_hours
        ]
        self._deferred = []
        return fresh

    @property
    def deferred_count(self) -> int:
        return len(self._deferred)

    def next_quiet_end(self, at: float | None = None) -> datetime | None:
        if not self.quiet_end:
            return None
        local = self._local_now(at)
        end = local.replace(
            hour=self.quiet_end.hour, minute=self.quiet_end.minute,
            second=0, microsecond=0,
        )
        if end <= local:
            end += timedelta(days=1)
        return end
