"""X / Twitter signals.

**Why this is built the way it is.** X's official filtered stream - the
endpoint you would actually want - is gated behind their Pro tier at $5,000
per month. Third-party mirrors resell the same data at roughly $0.15 per
1,000 tweets, which is what makes this project affordable at all.

But the naive design still does not fit a $20/month budget. Polling 60
accounts individually every 90 seconds is 57,600 requests a day; at a
one-tweet minimum billable unit per request that is about $8.60 a day, or
13x over budget.

The fix is to **batch**: X's search syntax accepts ``from:`` operators joined
with ``OR``, so 60 tracked accounts collapse into three search queries
instead of sixty timeline requests. That is a 20x cost reduction and it is
what brings a 60-second polling cadence inside the cap. :func:`estimate_cost`
prints the arithmetic for your own settings at startup.

On top of that the :class:`~memebot.sources.budget.BudgetGovernor` throttles
progressively as the month's spend approaches the cap, and stops entirely
rather than overspending.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..enrich.resolver import TokenResolver
from ..http import HttpClient
from ..models import Side, Signal, Source
from ..store import Store
from .base import SignalSource
from .budget import BudgetGovernor
from .promo import is_paid_promo, shill_score

log = logging.getLogger(__name__)


@dataclass
class TrackedAccount:
    handle: str
    score: float = 0.25
    last_tweet_id: str | None = None
    last_polled: float = 0.0
    tweets_seen: int = 0
    calls_made: int = 0
    promo_posts: int = 0

    @property
    def promo_fraction(self) -> float:
        return self.promo_posts / self.calls_made if self.calls_made else 0.0


def estimate_cost(
    n_accounts: int,
    poll_interval_seconds: float,
    *,
    batch_size: int = 20,
    usd_per_1k_tweets: float = 0.15,
    min_billable: int = 1,
    avg_tweets_per_response: float = 2.0,
) -> dict[str, float]:
    """Project monthly spend so the budget is a decision, not a surprise."""
    batches = max(1, -(-n_accounts // batch_size))
    requests_per_day = batches * (86400 / poll_interval_seconds)
    billable = max(min_billable, avg_tweets_per_response)
    daily = requests_per_day * billable * (usd_per_1k_tweets / 1000.0)
    return {
        "batches_per_poll": batches,
        "requests_per_day": round(requests_per_day),
        "usd_per_day": round(daily, 3),
        "usd_per_month": round(daily * 30.4, 2),
    }


class XSource(SignalSource):
    name = "x"

    def __init__(
        self,
        cfg: Config,
        store: Store,
        resolver: TokenResolver,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> None:
        super().__init__()
        x = cfg.section("sources").get("x", {})
        self.enabled = x.get("enabled", True)
        self.poll_interval = x.get("poll_interval_seconds", 90)
        self.min_interval = x.get("min_poll_interval_seconds", 45)
        self.max_interval = x.get("max_poll_interval_seconds", 900)
        self.max_accounts = x.get("max_tracked_accounts", 60)
        self.include_retweets = x.get("include_retweets", False)
        self.include_replies = x.get("include_replies", False)
        self.batch_size = x.get("batch_size", 20)

        self.api_key = api_key
        self.api_base = (api_base or "https://api.twitterapi.io").rstrip("/")
        self.store = store
        self.resolver = resolver

        b = x.get("budget", {})
        self.budget = BudgetGovernor(
            store,
            monthly_usd_cap=b.get("monthly_usd_cap", 20.0),
            usd_per_1k_tweets=b.get("usd_per_1k_tweets", 0.15),
            min_billable_per_request=b.get("min_billable_tweets_per_request", 1),
            soft_stop_fraction=b.get("soft_stop_fraction", 0.80),
            daily_pacing=b.get("daily_pacing", True),
        )

        self.http = HttpClient(
            rate=3.0,
            timeout=20.0,
            headers={"X-API-Key": api_key} if api_key else None,
        )
        self.accounts: dict[str, TrackedAccount] = {}
        self._seen_tweets: set[str] = set()
        self.on_budget_exhausted = None

    # --- tracked account management --------------------------------------
    def set_accounts(self, handles_scores: dict[str, float]) -> None:
        ranked = sorted(handles_scores.items(), key=lambda kv: -kv[1])[: self.max_accounts]
        keep = {h.lstrip("@") for h, _ in ranked}
        for h, s in ranked:
            key = h.lstrip("@")
            acc = self.accounts.get(key)
            if acc:
                acc.score = s
            else:
                self.accounts[key] = TrackedAccount(handle=key, score=s)
        for gone in set(self.accounts) - keep:
            self.accounts.pop(gone, None)
        log.info("X tracked accounts: %d", len(self.accounts))

    def cost_projection(self) -> dict[str, float]:
        return estimate_cost(
            len(self.accounts) or self.max_accounts,
            self.poll_interval,
            batch_size=self.batch_size,
            usd_per_1k_tweets=self.budget.price_per_tweet * 1000,
            min_billable=self.budget.min_billable,
        )

    # --- main loop --------------------------------------------------------
    async def run(self, out: asyncio.Queue[Signal]) -> None:
        if not self.enabled:
            log.info("X source disabled in config")
            return
        if not self.api_key:
            log.warning(
                "X source enabled but X_API_KEY is not set - staying dormant. "
                "Consensus will run on the remaining sources."
            )
            return

        proj = self.cost_projection()
        log.info(
            "X cost projection: %d accounts in %d batches every %ds "
            "= ~%d req/day = ~$%.2f/month (cap $%.2f)",
            len(self.accounts) or self.max_accounts, proj["batches_per_poll"],
            self.poll_interval, proj["requests_per_day"], proj["usd_per_month"],
            self.budget.cap,
        )
        if proj["usd_per_month"] > self.budget.cap:
            log.warning(
                "projected spend $%.2f exceeds the $%.2f cap - the governor "
                "will throttle. Reduce max_tracked_accounts or raise "
                "poll_interval_seconds to poll at full speed.",
                proj["usd_per_month"], self.budget.cap,
            )

        notified_exhausted = False
        while True:
            throttle = self.budget.throttle_multiplier()
            if throttle == float("inf"):
                if not notified_exhausted:
                    log.error("X budget exhausted: %s - pausing", self.budget.status())
                    if self.on_budget_exhausted:
                        await self._safe_notify()
                    notified_exhausted = True
                await asyncio.sleep(3600)
                continue
            notified_exhausted = False

            try:
                await self._poll_all(out)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("X poll cycle failed; continuing")

            interval = max(
                self.min_interval, min(self.max_interval, self.poll_interval * throttle)
            )
            await asyncio.sleep(interval)

    async def _safe_notify(self) -> None:
        try:
            res = self.on_budget_exhausted(self.budget.status())
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            log.exception("budget notifier failed")

    def _batches(self) -> list[list[TrackedAccount]]:
        accs = sorted(self.accounts.values(), key=lambda a: -a.score)
        return [
            accs[i : i + self.batch_size] for i in range(0, len(accs), self.batch_size)
        ]

    def build_query(self, batch: list[TrackedAccount]) -> str:
        """``(from:a OR from:b ...) -is:retweet -is:reply``.

        Batching is what makes the budget work: one request covers up to
        ``batch_size`` accounts instead of one each.
        """
        froms = " OR ".join(f"from:{a.handle}" for a in batch)
        q = f"({froms})"
        if not self.include_retweets:
            q += " -is:retweet"
        if not self.include_replies:
            q += " -is:reply"
        return q

    async def _poll_all(self, out: asyncio.Queue[Signal]) -> None:
        for batch in self._batches():
            if not self.budget.can_spend():
                log.warning("budget stop mid-cycle: %s", self.budget.status())
                return
            tweets = await self._search(self.build_query(batch))
            self.budget.record_request(len(tweets))
            for tweet in tweets:
                await self._handle_tweet(tweet, out)

    async def _search(self, query: str) -> list[dict[str, Any]]:
        data = await self.http.get_json(
            f"{self.api_base}/twitter/tweet/advanced_search",
            {"query": query, "queryType": "Latest"},
            use_cache=False,
        )
        if not data:
            return []
        # Providers differ in envelope shape; accept the common ones.
        for key in ("tweets", "data", "results"):
            val = data.get(key) if isinstance(data, dict) else None
            if isinstance(val, list):
                return val
        return data if isinstance(data, list) else []

    # --- tweet handling ---------------------------------------------------
    @staticmethod
    def _tweet_fields(t: dict[str, Any]) -> tuple[str, str, str, str | None]:
        tid = str(t.get("id") or t.get("id_str") or t.get("tweet_id") or "")
        text = t.get("text") or t.get("full_text") or t.get("content") or ""
        author = t.get("author") or t.get("user") or {}
        handle = (
            author.get("userName")
            or author.get("username")
            or author.get("screen_name")
            or t.get("username")
            or ""
        ).lstrip("@")
        url = t.get("url") or (
            f"https://x.com/{handle}/status/{tid}" if handle and tid else None
        )
        return tid, text, handle, url

    async def _handle_tweet(self, tweet: dict[str, Any], out: asyncio.Queue[Signal]) -> None:
        tid, text, handle, url = self._tweet_fields(tweet)
        if not text or not handle:
            return
        dedupe_key = tid or hashlib.sha1(f"{handle}{text}".encode()).hexdigest()
        if dedupe_key in self._seen_tweets:
            return
        self._seen_tweets.add(dedupe_key)
        if len(self._seen_tweets) > 50_000:
            self._seen_tweets = set(list(self._seen_tweets)[-25_000:])

        acc = self.accounts.get(handle)
        if acc is None:
            return
        acc.tweets_seen += 1
        acc.last_tweet_id = tid or acc.last_tweet_id
        acc.last_polled = time.time()

        promo = is_paid_promo(text)
        shill = shill_score(text)
        if promo:
            acc.promo_posts += 1

        resolutions = await self.resolver.resolve(text)
        if not resolutions:
            return

        # Someone naming five tokens in one post is not making a call.
        if len(resolutions) > 3:
            log.debug("@%s mentioned %d tokens in one post - ignoring", handle, len(resolutions))
            return

        side = self._infer_side(text)

        for res in resolutions:
            acc.calls_made += 1
            confidence = res.confidence
            if promo:
                confidence *= 0.2
            if shill:
                confidence *= max(0.3, 1.0 - shill)

            # Record for later grading; this is what turns a handle into a
            # score over the following weeks.
            call_id = hashlib.sha1(f"{dedupe_key}:{res.mint}".encode()).hexdigest()[:16]
            self.store.record_x_call(
                call_id, handle, res.mint,
                res.market.price_usd if res.market else None,
                None,
            )

            await self.emit(
                out,
                Signal(
                    source=Source.X,
                    actor_id=handle,
                    token_mint=res.mint,
                    token_symbol=res.symbol,
                    side=side,
                    size_usd=None,       # social posts carry no position size
                    price_usd=res.market.price_usd if res.market else None,
                    actor_score=acc.score,
                    confidence=round(confidence, 3),
                    url=url,
                    raw={"text": text[:500], "resolution": res.method,
                         "alternatives": res.alternatives, "promo": promo},
                ),
            )

    @staticmethod
    def _infer_side(text: str) -> Side:
        """Crude but useful: distinguish a call from an exit announcement.

        Defaults to BUY, because the overwhelming majority of CA posts are
        entries, and a missed sell only costs a signal while a misread sell
        would actively poison the consensus for a token.
        """
        lowered = text.lower()
        sell_markers = (
            "sold", "selling", "took profit", "taking profit", "out of",
            "exited", "exiting", "dumped", "trimmed", "closed my",
        )
        buy_markers = ("bought", "buying", "aped", "aping", "added", "entry", "long")
        sell_hit = any(m in lowered for m in sell_markers)
        buy_hit = any(m in lowered for m in buy_markers)
        if sell_hit and not buy_hit:
            return Side.SELL
        return Side.BUY

    async def close(self) -> None:
        await self.http.close()
