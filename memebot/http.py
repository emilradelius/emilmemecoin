"""Shared async HTTP helper: retries, token-bucket rate limiting, TTL cache.

All third-party APIs here are free or cheap-but-metered, and all of them will
rate-limit or briefly 5xx under load. Every call in the enrichment layer goes
through :class:`HttpClient` so that a flaky upstream degrades into a ``None``
return rather than an exception that kills a source task.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class RateLimiter:
    """Simple token bucket. ``rate`` is requests per second."""

    def __init__(self, rate: float, burst: int | None = None) -> None:
        self.rate = rate
        self.capacity = burst if burst is not None else max(1, int(rate * 2))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate)


class _TTLCache:
    def __init__(self, ttl: float, maxsize: int = 4096) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._d.get(key)
        if hit is None:
            return None
        ts, val = hit
        if time.monotonic() - ts > self.ttl:
            self._d.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any) -> None:
        if len(self._d) >= self.maxsize:
            # Cheap eviction: drop the oldest quarter.
            for k in sorted(self._d, key=lambda k: self._d[k][0])[: self.maxsize // 4]:
                self._d.pop(k, None)
        self._d[key] = (time.monotonic(), val)


class HttpClient:
    def __init__(
        self,
        *,
        rate: float = 5.0,
        timeout: float = 12.0,
        retries: int = 3,
        cache_ttl: float = 0.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._limiter = RateLimiter(rate)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retries = retries
        self._cache = _TTLCache(cache_ttl) if cache_ttl > 0 else None
        self._headers = headers or {}
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "HttpClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, headers=self._headers
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_json(
        self, url: str, params: dict[str, Any] | None = None, *, use_cache: bool = True
    ) -> Any | None:
        """GET returning parsed JSON, or ``None`` on any persistent failure.

        Callers must handle ``None``. Enrichment is best-effort: a token we
        cannot verify is a token we reject, which is the safe direction.
        """
        cache_key = f"{url}?{sorted((params or {}).items())}"
        if self._cache and use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        await self.start()
        assert self._session is not None

        delay = 0.5
        for attempt in range(self._retries):
            try:
                await self._limiter.acquire()
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", delay))
                        log.debug("429 from %s, sleeping %.1fs", url, retry_after)
                        await asyncio.sleep(min(retry_after, 30.0))
                        delay *= 2
                        continue
                    if resp.status >= 500:
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    if resp.status == 404:
                        return None
                    if resp.status >= 400:
                        log.debug("HTTP %s from %s", resp.status, url)
                        return None
                    data = await resp.json(content_type=None)
                    if self._cache:
                        self._cache.set(cache_key, data)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.debug("request to %s failed (attempt %d): %s", url, attempt + 1, exc)
                await asyncio.sleep(delay)
                delay *= 2
        return None

    async def post_json(
        self, url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None
    ) -> Any | None:
        await self.start()
        assert self._session is not None
        delay = 0.5
        for _ in range(self._retries):
            try:
                await self._limiter.acquire()
                async with self._session.post(url, json=payload, headers=headers) as resp:
                    if resp.status >= 500 or resp.status == 429:
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    if resp.status >= 400:
                        log.debug("HTTP %s from %s", resp.status, url)
                        return None
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.debug("post to %s failed: %s", url, exc)
                await asyncio.sleep(delay)
                delay *= 2
        return None
