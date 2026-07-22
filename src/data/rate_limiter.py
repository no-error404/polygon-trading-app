"""
rate_limiter.py — Async token-bucket rate limiter for API calls.

Prevents getting banned by Polymarket Gamma/CLOB or Open-Meteo APIs.
Uses a sliding-window approach: tracks timestamps of recent calls and
ensures we never exceed N requests per T seconds per API host.

Two layers:
  1. TokenBucket — coarse rate (max N calls per T seconds)
  2. MinInterval — minimum gap between consecutive calls (burst protection)

Both are async and designed to be used as context managers or via acquire().
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


class RateLimitError(RuntimeError):
    """Raised when a rate-limited call would block beyond its timeout."""


@dataclass
class TokenBucketRateLimiter:
    """
    Sliding-window rate limiter: max `max_calls` per `window_seconds`.

    Usage:
        limiter = TokenBucketRateLimiter(max_calls=4000, window_seconds=10)
        await limiter.acquire()  # blocks until a slot is available
        response = await httpx_client.get(url)
    """
    max_calls: int = 4000
    window_seconds: float = 10.0
    _timestamps: deque[float] = field(default_factory=deque)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, timeout: float = 30.0) -> None:
        """Acquire a rate-limit slot, blocking if necessary."""
        deadline = time.monotonic() + timeout

        async with self._lock:
            now = time.monotonic()
            # Evict expired timestamps
            cutoff = now - self.window_seconds
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) < self.max_calls:
                self._timestamps.append(now)
                return

            # Need to wait for the oldest timestamp to expire
            wait_time = self._timestamps[0] + self.window_seconds - now
            if time.monotonic() + wait_time > deadline:
                raise RateLimitError(
                    f"Rate limit ({self.max_calls}/{self.window_seconds}s) "
                    f"exceeded; would wait {wait_time:.1f}s > {timeout}s timeout"
                )

            await asyncio.sleep(wait_time)
            # Re-check after sleeping
            now = time.monotonic()
            cutoff = now - self.window_seconds
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()
            self._timestamps.append(now)

    @property
    def current_usage(self) -> int:
        """Current number of calls in the rolling window."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        # Count without mutating (read-only)
        count = 0
        for ts in self._timestamps:
            if ts > cutoff:
                count += 1
        return count


@dataclass
class MinIntervalLimiter:
    """
    Enforces a minimum gap between consecutive calls (burst protection).

    Usage:
        limiter = MinIntervalLimiter(min_interval=0.1)  # 10 req/s max
        await limiter.acquire()
    """
    min_interval: float = 0.1  # seconds between calls
    _last_call: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                wait = self.min_interval - elapsed
                if time.monotonic() + wait > deadline:
                    raise RateLimitError(
                        f"Min interval {self.min_interval}s not met; "
                        f"would wait {wait:.3f}s"
                    )
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


# Pre-configured limiters per API (conservative defaults)
# Gamma: 4000 req / 10s -> we use 1000 to be safe
# CLOB:  9000 req / 10s -> we use 2000 to be safe
# Open-Meteo: no hard limit published -> 10 req/s
GAMMA_LIMITER = TokenBucketRateLimiter(max_calls=1000, window_seconds=10.0)
CLOB_LIMITER = TokenBucketRateLimiter(max_calls=2000, window_seconds=10.0)
WEATHER_LIMITER = TokenBucketRateLimiter(max_calls=100, window_seconds=10.0)

# Burst protection (min gap between consecutive calls to same host)
GAMMA_BURST = MinIntervalLimiter(min_interval=0.05)   # 20 req/s burst cap
CLOB_BURST = MinIntervalLimiter(min_interval=0.05)
WEATHER_BURST = MinIntervalLimiter(min_interval=0.1)   # 10 req/s for Open-Meteo


async def gamma_rate_gate() -> None:
    """Apply both rate + burst limiters for Gamma API."""
    await GAMMA_BURST.acquire()
    await GAMMA_LIMITER.acquire()


async def clob_rate_gate() -> None:
    """Apply both rate + burst limiters for CLOB API."""
    await CLOB_BURST.acquire()
    await CLOB_LIMITER.acquire()


async def weather_rate_gate() -> None:
    """Apply both rate + burst limiters for Open-Meteo API."""
    await WEATHER_BURST.acquire()
    await WEATHER_LIMITER.acquire()