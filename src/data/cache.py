"""
cache.py — Async TTL (time-to-live) cache for API responses.

Avoids re-fetching the same data within a TTL window. Two cache tiers:
  1. In-memory: fast, per-process, cleared on restart
  2. File-based: optional, persists across restarts (JSON)

Usage as a decorator or direct:
    cache = TTLCache(default_ttl_seconds=60)

    @cache.cached(ttl_seconds=30)
    async def fetch_weather(lat, lon):
        ...

    # Or directly:
    data = await cache.get_or_set("key", fetch_fn, ttl=60)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


class CacheError(RuntimeError):
    """Raised when cache file operations fail."""


@dataclass
class CacheEntry:
    """A single cached response with expiry timestamp."""
    value: Any
    expires_at: float  # monotonic timestamp
    fetched_at: str    # ISO timestamp for audit


@dataclass
class TTLCache:
    """
    Async TTL cache with optional file persistence.

    Args:
        default_ttl_seconds: default TTL if not specified per-call
        file_cache_dir: if set, persists cache entries as JSON files
    """
    default_ttl_seconds: float = 60.0
    file_cache_dir: Optional[str] = None
    _store: dict[str, CacheEntry] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _hits: int = 0
    _misses: int = 0

    def _make_key(self, *args, **kwargs) -> str:
        """Hash args+kwargs into a stable cache key."""
        raw = json.dumps(
            {"args": args, "kwargs": kwargs},
            sort_keys=True, default=str
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def get_or_set(
        self,
        key: str,
        fetch_fn: Callable[[], Awaitable[Any]],
        ttl_seconds: Optional[float] = None,
    ) -> Any:
        """
        Return cached value if fresh, else call fetch_fn and cache result.

        Args:
            key: cache key (use _make_key for automatic key generation)
            fetch_fn: async callable that fetches fresh data
            ttl_seconds: override default TTL for this entry
        """
        ttl = ttl_seconds or self.default_ttl_seconds

        async with self._lock:
            now = time.monotonic()
            if key in self._store:
                entry = self._store[key]
                if now < entry.expires_at:
                    self._hits += 1
                    return entry.value
                # Expired — remove
                del self._store[key]

        # Fetch outside the lock to allow concurrent fetches for different keys
        self._misses += 1
        value = await fetch_fn()

        async with self._lock:
            from datetime import datetime, timezone
            self._store[key] = CacheEntry(
                value=value,
                expires_at=time.monotonic() + ttl,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )

        # Optionally persist to file
        if self.file_cache_dir:
            self._persist_to_file(key, value, ttl)

        return value

    def cached(
        self, ttl_seconds: Optional[float] = None
    ) -> Callable:
        """
        Decorator for async functions. Caches based on args+kwargs.

        Usage:
            cache = TTLCache(default_ttl_seconds=30)
            @cache.cached(ttl_seconds=15)
            async def fetch_weather(lat, lon):
                ...
        """
        def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            async def wrapper(*args, **kwargs) -> Any:
                key = self._make_key(fn.__name__, *args, **kwargs)
                return await self.get_or_set(key, lambda: fn(*args, **kwargs), ttl_seconds)
            wrapper.__name__ = fn.__name__
            wrapper.__doc__ = fn.__doc__
            return wrapper
        return decorator

    def _persist_to_file(self, key: str, value: Any, ttl: float) -> None:
        """Save cache entry to file (best-effort, non-fatal on failure)."""
        if not self.file_cache_dir:
            return
        try:
            cache_dir = Path(self.file_cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / f"{key}.json"
            from datetime import datetime, timezone
            payload = {
                "key": key,
                "value": value,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": ttl,
            }
            with open(cache_file, "w") as f:
                json.dump(payload, f, default=str)
        except (OSError, TypeError) as e:
            # File cache failure is non-fatal — in-memory cache still works
            pass

    def stats(self) -> dict[str, int]:
        """Return cache hit/miss statistics."""
        return {"hits": self._hits, "misses": self._misses,
                "size": len(self._store)}

    async def clear(self) -> None:
        """Clear all cached entries."""
        async with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0


# Global cache instances with different TTLs for different data types
# Market discovery: 5 min TTL (new markets appear infrequently)
GAMMA_CACHE = TTLCache(default_ttl_seconds=300.0)

# Orderbook: 30s TTL (prices move fast on active markets)
CLOB_CACHE = TTLCache(default_ttl_seconds=30.0)

# Weather forecast: 60 min TTL (models update every 6h)
WEATHER_CACHE = TTLCache(default_ttl_seconds=3600.0)