"""Price your X polling settings before you pay for them.

Run: ``python -m memebot.tools.cost``
"""

from __future__ import annotations

import sys

from ..config import Config
from ..sources.xfeed import estimate_cost


def main() -> int:
    cfg = Config.load()
    x = cfg.section("sources").get("x", {})
    b = x.get("budget", {})
    cap = b.get("monthly_usd_cap", 20.0)

    n = x.get("max_tracked_accounts", 40)
    interval = x.get("poll_interval_seconds", 120)
    batch = x.get("batch_size", 20)
    price = b.get("usd_per_1k_tweets", 0.15)

    current = estimate_cost(n, interval, batch_size=batch, usd_per_1k_tweets=price)
    naive = estimate_cost(n, interval, batch_size=1, usd_per_1k_tweets=price)

    print(f"Configured: {n} accounts, {interval}s interval, batches of {batch}")
    print(f"  {current['batches_per_poll']:.0f} requests per poll cycle")
    print(f"  {current['requests_per_day']:,.0f} requests/day")
    print(f"  ${current['usd_per_day']:.2f}/day  ->  ${current['usd_per_month']:.2f}/month")
    print(f"  Monthly cap: ${cap:.2f}", end="  ")
    print("OK" if current["usd_per_month"] <= cap else "*** OVER CAP ***")
    print(f"\nWithout batching this would be ${naive['usd_per_month']:.2f}/month "
          f"({naive['usd_per_month'] / max(current['usd_per_month'], 0.01):.0f}x more).")

    print("\nAlternatives:")
    for accounts in (20, 40, 60, 80):
        for iv in (60, 120, 300):
            e = estimate_cost(accounts, iv, batch_size=batch, usd_per_1k_tweets=price)
            flag = "" if e["usd_per_month"] <= cap else "  (over cap)"
            print(f"  {accounts:>3} accounts @ {iv:>3}s -> ${e['usd_per_month']:>6.2f}/mo{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
