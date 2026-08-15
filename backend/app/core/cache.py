"""Small async TTL cache for outbound fetches.

Written for /api/chart, which re-fetched Binance klines on every single chart
open — no cache at all, ~0.35s and one outbound request per view, per user, per
symbol switch.

Deliberately simpler than the stale-while-revalidate cache inside
api/liquidity.py: that one guards CoinGecko calls slow enough that serving a
stale value beats making the user wait, and it pays for that with background
refresh tasks. A kline fetch is a third of a second, so a plain TTL plus
stampede protection covers it without the moving parts.

(api/liquidity.py keeps its own copy of this machinery. Consolidating the two
is worthwhile but means editing the file that serves the live screener, so it
is left for a change that can be verified on its own.)
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Per-key TTL cache with single-flight refresh.

    Locks are per key, never global: a slow fetch for one symbol must not
    serialize every other symbol behind it.
    """

    def __init__(self, ttl: float, max_entries: int = 512) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._values: dict[str, tuple[float, T]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _evict_if_needed(self) -> None:
        # Keys are (symbol, interval, limit) combinations, so the space is
        # bounded in practice — but a caller passing arbitrary limits could
        # still grow it. Drop the oldest entries rather than let it run away.
        if len(self._values) <= self._max_entries:
            return
        overflow = len(self._values) - self._max_entries
        oldest = sorted(self._values, key=lambda k: self._values[k][0])[:overflow]
        for key in oldest:
            self._values.pop(key, None)
            self._locks.pop(key, None)

    async def get(self, key: str, fetch: Callable[[], Awaitable[T]]) -> T:
        hit = self._values.get(key)
        if hit is not None and time.time() - hit[0] < self._ttl:
            return hit[1]

        async with self._lock_for(key):
            # Re-check: while we waited for the lock another caller may have
            # refreshed this key, and a second identical fetch is exactly what
            # the lock exists to prevent.
            hit = self._values.get(key)
            if hit is not None and time.time() - hit[0] < self._ttl:
                return hit[1]

            value = await fetch()
            self._values[key] = (time.time(), value)
            self._evict_if_needed()
            return value

    def invalidate(self) -> None:
        self._values.clear()
        self._locks.clear()

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._values), "locks": len(self._locks)}
