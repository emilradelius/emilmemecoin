"""Spend governor for metered APIs.

The X data feed is the only component here that costs real money per request,
and it is trivially easy to burn a month's budget in a day by polling sixty
accounts every thirty seconds. This tracks estimated spend against a hard
monthly cap and degrades in stages rather than either overspending silently
or dying at 100%.

Spend is *estimated*, not billed - it assumes the provider's advertised
per-tweet price and a minimum billable unit per request. Reconcile against
your provider dashboard occasionally; the estimate is deliberately
conservative (it rounds against you) so that reality should come in under it.
"""

from __future__ import annotations

import calendar
import logging
import time
from datetime import datetime, timezone

from ..store import Store

log = logging.getLogger(__name__)


class BudgetGovernor:
    KEY = "x_budget"

    def __init__(
        self,
        store: Store,
        *,
        monthly_usd_cap: float = 20.0,
        usd_per_1k_tweets: float = 0.15,
        min_billable_per_request: int = 1,
        soft_stop_fraction: float = 0.80,
        daily_pacing: bool = True,
    ) -> None:
        self.store = store
        self.cap = monthly_usd_cap
        self.price_per_tweet = usd_per_1k_tweets / 1000.0
        self.min_billable = min_billable_per_request
        self.soft_stop = soft_stop_fraction
        self.daily_pacing = daily_pacing

    # --- state -----------------------------------------------------------
    @staticmethod
    def _period() -> str:
        now = datetime.now(timezone.utc)
        return f"{now.year}-{now.month:02d}"

    def _state(self) -> dict:
        st = self.store.kv_get(self.KEY) or {}
        if st.get("period") != self._period():
            # New month, fresh budget.
            st = {"period": self._period(), "spent_usd": 0.0, "requests": 0,
                  "tweets": 0, "stopped": False}
            self.store.kv_set(self.KEY, st)
        return st

    @property
    def spent(self) -> float:
        return float(self._state().get("spent_usd", 0.0))

    @property
    def remaining(self) -> float:
        return max(0.0, self.cap - self.spent)

    @property
    def fraction_used(self) -> float:
        return self.spent / self.cap if self.cap > 0 else 1.0

    def record_request(self, tweets_returned: int) -> float:
        """Bill a completed request. Returns the marginal cost."""
        billable = max(self.min_billable, tweets_returned)
        cost = billable * self.price_per_tweet
        st = self._state()
        st["spent_usd"] = round(float(st.get("spent_usd", 0.0)) + cost, 6)
        st["requests"] = int(st.get("requests", 0)) + 1
        st["tweets"] = int(st.get("tweets", 0)) + tweets_returned
        self.store.kv_set(self.KEY, st)
        return cost

    # --- policy ----------------------------------------------------------
    def _pace_allowance(self) -> float:
        """How much of the cap we *should* have spent by now this month.

        Without pacing, a busy first week can exhaust the budget and leave the
        bot blind for the rest of the month - which is exactly when you would
        not notice it had stopped working.
        """
        if not self.daily_pacing:
            return self.cap
        now = datetime.now(timezone.utc)
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        elapsed = (now.day - 1) + (now.hour * 3600 + now.minute * 60) / 86400.0
        # Allow a 25% overshoot on the straight line so a genuinely busy day
        # is not throttled, but a runaway is.
        return self.cap * min(1.0, (elapsed + 1) / days_in_month) * 1.25

    def can_spend(self) -> bool:
        return self.remaining > 0 and self.spent < self._pace_allowance()

    def throttle_multiplier(self) -> float:
        """Factor to multiply poll intervals by. 1.0 = full speed."""
        if not self.can_spend():
            return float("inf")
        used = self.fraction_used
        if used < self.soft_stop:
            # Also throttle if we are ahead of pace even while under the soft
            # stop, so spend stays roughly linear across the month.
            pace = self._pace_allowance()
            if pace > 0 and self.spent > pace * 0.8:
                return 1.5
            return 1.0
        # Between the soft stop and the cap, slow down progressively.
        headroom = max(0.0, (1.0 - used) / (1.0 - self.soft_stop))
        return 1.0 + (1.0 - headroom) * 5.0

    def status(self) -> str:
        st = self._state()
        return (
            f"${self.spent:.2f} / ${self.cap:.2f} this month "
            f"({self.fraction_used:.0%}), {st.get('requests', 0):,} requests, "
            f"{st.get('tweets', 0):,} tweets"
        )
