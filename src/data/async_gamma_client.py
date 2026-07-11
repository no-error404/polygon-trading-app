"""
async_gamma_client.py — Async httpx client for Polymarket Gamma + CLOB APIs.

Fetches live contract order book states: token_id, highest bid, lowest ask,
volume — concurrently across all buckets in a weather event.

Uses asyncio + httpx for concurrent I/O. Rate-limited and cached.

Key design:
  - Gamma API: discover events and their bracket markets (read-only)
  - CLOB API: fetch orderbooks per token (read-only GET /book)
  - All bracket buckets fetched CONCURRENTLY via asyncio.gather
  - Rate-limited via rate_limiter module
  - Cached via cache module (Gamma 5min, CLOB 30s)

Edge cases handled:
  - Double-encoded JSON fields (outcomePrices, clobTokenIds) — json.loads()
  - Empty clobTokenIds → skip market, log warning
  - 404 on closed markets → return None, don't crash the gather
  - httpx timeout, connect error, status error — specific exception types
  - Non-weather false positives filtered by keyword relevance
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

import httpx

from .rate_limiter import gamma_rate_gate, clob_rate_gate
from .cache import GAMMA_CACHE, CLOB_CACHE

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Keywords that indicate a genuine weather market
WEATHER_KEYWORDS = [
    "temperature", "highest temp", "max temp", "snowfall", "snow",
    "rainfall", "precipitation", "wind speed", "humidity",
    "heat index", "wind chill", "space weather",
]

_FALSE_POSITIVE_PATTERNS = [
    re.compile(r"\bsnow\b.*(?:earnings|stock|quarterly|nasdaq|nyse)", re.IGNORECASE),
    re.compile(r"senate|primary|election", re.IGNORECASE),
    re.compile(r"player of the year|award|medal", re.IGNORECASE),
    re.compile(r"beast games|game show", re.IGNORECASE),
]


# --- Structural type hints (Protocols) ---

@runtime_checkable
class AsyncHttpCallable(Protocol):
    """Structural type for any async HTTP client that can GET."""
    async def get(self, url: str, *, params: dict[str, Any],
                  timeout: float) -> httpx.Response: ...


# --- Data models ---

@dataclass
class ContractOrderBook:
    """Orderbook state for a single YES/NO contract (one bracket bucket)."""
    token_id: str
    condition_id: str
    question: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: Optional[float]
    midpoint: Optional[float]
    bid_depth: float            # total size at top-3 bid levels
    ask_depth: float            # total size at top-3 ask levels
    last_trade_price: Optional[float]
    min_order_size: float
    tick_size: float
    volume: float
    fetched_at: str            # ISO timestamp


@dataclass
class WeatherEventMarket:
    """A weather event with all bracket bucket orderbooks resolved."""
    event_id: str
    event_title: str
    event_slug: str
    event_volume: float
    event_description: str
    active: bool
    closed: bool
    contracts: list[ContractOrderBook] = field(default_factory=list)
    # Bucket metadata for strategy layer
    bucket_labels: list[str] = field(default_factory=list)
    outcome_prices: list[list[float]] = field(default_factory=list)


def _is_weather_event(title: str, description: str = "") -> bool:
    """Filter genuine weather events from false positives."""
    text = f"{title} {description}".lower()
    for pattern in _FALSE_POSITIVE_PATTERNS:
        if pattern.search(text):
            return False
    return any(kw in text for kw in WEATHER_KEYWORDS)


def _parse_double_encoded(field_val: Any) -> list:
    """Parse a double-encoded JSON string field from Gamma API."""
    if not field_val:
        return []
    if isinstance(field_val, list):
        return field_val
    if isinstance(field_val, str):
        try:
            return json.loads(field_val)
        except json.JSONDecodeError:
            return []
    return []


class AsyncGammaClient:
    """
    Async httpx client for Polymarket Gamma + CLOB APIs.

    Concurrently fetches:
      1. Weather event discovery from Gamma (search + filter)
      2. Per-bucket orderbooks from CLOB (all buckets in parallel)

    All calls are rate-limited and cached.
    """

    def __init__(
        self,
        gamma_base: str = GAMMA_BASE,
        clob_base: str = CLOB_BASE,
        timeout: float = 30.0,
        max_concurrent_clob: int = 10,
    ) -> None:
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent_clob)

    async def __aenter__(self) -> AsyncGammaClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"User-Agent": "polymarket-weather-trader/2.0",
                     "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._client.aclose()

    async def _gamma_get(self, path: str, params: dict[str, Any]) -> Any:
        """Rate-limited, cached GET to Gamma API."""
        url = f"{self.gamma_base}{path}"

        async def _fetch() -> Any:
            await gamma_rate_gate()
            resp = await self._client.get(url, params=params,
                                          timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()

        # Cache key from URL + params
        cache_key = f"gamma:{url}:{json.dumps(params, sort_keys=True)}"
        return await GAMMA_CACHE.get_or_set(cache_key, _fetch, ttl_seconds=300)

    async def _clob_get(self, path: str, params: dict[str, Any]) -> Any:
        """Rate-limited, cached GET to CLOB API."""
        url = f"{self.clob_base}{path}"

        async def _fetch() -> Any:
            await clob_rate_gate()
            resp = await self._client.get(url, params=params,
                                          timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()

        cache_key = f"clob:{url}:{json.dumps(params, sort_keys=True)}"
        return await CLOB_CACHE.get_or_set(cache_key, _fetch, ttl_seconds=30)

    async def discover_weather_events(
        self,
        active_only: bool = False,
        limit: int = 50,
    ) -> list[WeatherEventMarket]:
        """
        Discover weather events on Polymarket via Gamma search.

        Searches multiple terms, deduplicates, and filters false positives.
        Returns WeatherEventMarket objects (without orderbooks yet —
        call fetch_orderbooks() to fill those in).
        """
        search_terms = ["temperature", "weather", "snow", "rainfall",
                         "space weather"]
        seen_slugs: set[str] = set()
        events: list[WeatherEventMarket] = []

        # Search all terms concurrently
        async def search_one(term: str) -> list[dict]:
            data = await self._gamma_get("/public-search", {"q": term})
            return data.get("events", [])

        results = await asyncio.gather(
            *(search_one(t) for t in search_terms),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Gamma search failed: {result}")
                continue
            for event_raw in result:
                title = event_raw.get("title", "")
                desc = event_raw.get("description", "") or ""
                slug = event_raw.get("slug", "")

                if slug in seen_slugs:
                    continue
                if not _is_weather_event(title, desc):
                    continue
                if active_only and (
                    not event_raw.get("active", False)
                    or event_raw.get("closed", False)
                ):
                    continue

                seen_slugs.add(slug)
                events.append(WeatherEventMarket(
                    event_id=str(event_raw.get("id", "")),
                    event_title=title,
                    event_slug=slug,
                    event_volume=float(event_raw.get("volume", 0) or 0),
                    event_description=desc,
                    active=bool(event_raw.get("active", False)),
                    closed=bool(event_raw.get("closed", False)),
                ))

        return events

    async def fetch_event_markets(self, event_slug: str) -> list[dict]:
        """Fetch raw market data for a specific event by slug."""
        data = await self._gamma_get("/events", {"slug": event_slug})
        if isinstance(data, list) and len(data) > 0:
            return data[0].get("markets", []) or []
        return []

    async def fetch_orderbook(
        self,
        token_id: str,
        question: str = "",
        condition_id: str = "",
        volume: float = 0.0,
    ) -> Optional[ContractOrderBook]:
        """
        Fetch a single token's orderbook from CLOB.
        Returns None on 404 (closed market) or other API error.
        """
        async with self._semaphore:
            try:
                data = await self._clob_get("/book", {"token_id": token_id})
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.debug(f"CLOB 404 for token {token_id} (closed market)")
                    return None
                logger.warning(f"CLOB HTTP error for {token_id}: {e}")
                return None
            except httpx.RequestError as e:
                logger.warning(f"CLOB request error for {token_id}: {e}")
                return None

        from datetime import datetime, timezone

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        spread = (best_ask - best_bid) if (best_bid is not None
                   and best_ask is not None) else None
        midpoint = ((best_bid + best_ask) / 2.0) if spread is not None else None

        return ContractOrderBook(
            token_id=token_id,
            condition_id=condition_id or data.get("market", ""),
            question=question,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            midpoint=midpoint,
            bid_depth=sum(float(b["size"]) for b in bids[:3]),
            ask_depth=sum(float(a["size"]) for a in asks[:3]),
            last_trade_price=float(data.get("last_trade_price", 0)) or None,
            min_order_size=float(data.get("min_order_size", 5)),
            tick_size=float(data.get("tick_size", 0.01)),
            volume=volume,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    async def fetch_event_orderbooks(
        self,
        event: WeatherEventMarket,
    ) -> WeatherEventMarket:
        """
        Fetch ALL bracket bucket orderbooks for a weather event concurrently.

        Parses the event's markets from Gamma, extracts clobTokenIds,
        then fetches /book for each YES token in parallel.

        For closed markets where CLOB /book returns 404, a fallback
        ContractOrderBook is created from Gamma's outcomePrices so the
        pipeline still produces DataFrame rows (with None bid/ask depth).
        """
        markets_raw = await self.fetch_event_markets(event.event_slug)

        # Collect market metadata for fallback
        market_meta: list[dict] = []  # {token_id, question, condition_id, volume, prices, label}

        for m in markets_raw:
            clob_tokens = _parse_double_encoded(m.get("clobTokenIds", "[]"))
            if not clob_tokens or len(clob_tokens) < 2:
                logger.debug(f"Skipping market (no tokens): "
                             f"{m.get('question', '?')}")
                continue

            prices_raw = _parse_double_encoded(m.get("outcomePrices", "[]"))
            try:
                prices = [float(p) for p in prices_raw]
            except (ValueError, TypeError):
                prices = []

            yes_token = clob_tokens[0]
            question = m.get("question", "")
            condition_id = m.get("conditionId", "") or ""
            volume = float(m.get("volume", 0) or 0)
            label = _extract_bucket_label(question)

            market_meta.append({
                "token_id": yes_token,
                "question": question,
                "condition_id": condition_id,
                "volume": volume,
                "prices": prices,
                "label": label,
            })

        # Fetch all orderbooks concurrently
        tasks = [
            asyncio.ensure_future(
                self.fetch_orderbook(
                    mm["token_id"], mm["question"],
                    mm["condition_id"], mm["volume"]
                )
            )
            for mm in market_meta
        ]
        orderbooks = await asyncio.gather(*tasks, return_exceptions=True)

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        for mm, ob in zip(market_meta, orderbooks):
            if isinstance(ob, ContractOrderBook):
                event.contracts.append(ob)
                event.bucket_labels.append(mm["label"])
                event.outcome_prices.append(mm["prices"])
            elif isinstance(ob, Exception):
                logger.warning(f"Orderbook fetch failed: {ob}")
            elif ob is None:
                # CLOB 404 (closed market) — create fallback from Gamma prices
                prices = mm["prices"]
                yes_price = prices[0] if prices else 0.0
                no_price = prices[1] if len(prices) > 1 else 1.0 - yes_price

                # For resolved markets, the price IS the final settlement
                # (1.0 for winning bucket, 0.0 for losing)
                # For pre-resolution closed markets, it's the last traded price
                event.contracts.append(ContractOrderBook(
                    token_id=mm["token_id"],
                    condition_id=mm["condition_id"],
                    question=mm["question"],
                    best_bid=None,
                    best_ask=None,
                    spread=None,
                    midpoint=yes_price if yes_price > 0 else None,
                    bid_depth=0.0,
                    ask_depth=0.0,
                    last_trade_price=yes_price if yes_price > 0 else None,
                    min_order_size=5.0,
                    tick_size=0.01,
                    volume=mm["volume"],
                    fetched_at=now,
                ))
                event.bucket_labels.append(mm["label"])
                event.outcome_prices.append(mm["prices"])

        return event

    async def discover_and_fetch(
        self,
        active_only: bool = False,
    ) -> list[WeatherEventMarket]:
        """
        Full pipeline: discover weather events + fetch all orderbooks.
        Returns events with fully populated ContractOrderBook lists.
        """
        events = await self.discover_weather_events(active_only=active_only)
        # Fetch orderbooks for all events concurrently
        events = await asyncio.gather(
            *(self.fetch_event_orderbooks(e) for e in events),
            return_exceptions=True,
        )
        # Filter out exceptions
        result: list[WeatherEventMarket] = []
        for e in events:
            if isinstance(e, WeatherEventMarket):
                result.append(e)
            elif isinstance(e, Exception):
                logger.warning(f"Event fetch failed: {e}")
        return result


def _extract_bucket_label(question: str) -> str:
    """
    Extract the temperature bracket label from a market question.
    e.g. "Will the highest temperature ... be 86-87°F?" -> "86-87"
         "Will the highest temperature ... be 77°F or below?" -> "<=77"
         "Will the highest temperature ... be 96°F or above?" -> ">=96"
    """
    q = question.lower()
    if "or below" in q or "or less" in q:
        match = re.search(r"(\d+(?:\.\d+)?)", question)
        if match:
            return f"<={match.group(1)}"
    if "or above" in q or "or more" in q or "or higher" in q:
        match = re.search(r"(\d+(?:\.\d+)?)", question)
        if match:
            return f">={match.group(1)}"
    # Range: "86-87" or "86 to 87" or "86°F to 87°F"
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(\d+(?:\.\d+)?)", question)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    # Single value
    match = re.search(r"(\d+(?:\.\d+)?)", question)
    if match:
        return match.group(1)
    return question[:30]