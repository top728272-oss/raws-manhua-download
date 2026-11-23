"""Async token-bucket rate limiter.

Used to throttle source API calls (search/detail/chapters/pages metadata) to
a polite rate (~15/min by default). Image downloads are NOT limited here - those
are bounded by the download engine's concurrency cap instead.

The bucket refills continuously: `max_calls` tokens over `per_seconds`. Acquiring
a token blocks (asyncio-friendly) until one is available.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class AsyncLimiter:
    """A simple continuous-refill token bucket for asyncio."""

    def __init__(self, max_calls: int, per_seconds: float = 60.0) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be > 0")
        self._capacity = float(max_calls)
        self._per = float(per_seconds)
        self._rate = self._capacity / self._per
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        logger.debug(
            "AsyncLimiter: %d calls / %.0fs (%.3f tok/s)",
            max_calls,
            per_seconds,
            self._rate,
        )

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated = now

    async def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                deficit = 1.0 - self._tokens
                wait = deficit / self._rate
            logger.debug("rate limited; sleeping %.2fs", wait)
            await asyncio.sleep(wait)

    async def __aenter__(self) -> AsyncLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        return None
