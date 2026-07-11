"""
Gamma Client — read-only Polymarket market discovery.

Gamma API (gamma-api.polymarket.com) is public, no auth.
We use it for:
  1. Discovering weather events by keyword search.
  2. Fetching full event/market details including resolution descriptions.
  3. Extracting clobTokenIds, conditionIds, outcomePrices for CLOB queries.

CRITICAL EDGE CASES:
  - outcomePrices, outcomes, clobTokenIds are JSON STRINGS inside JSON
    (double-encoded). Must json.loads() them before use.
  - Some markets have empty clobTokenIds → skip and log.
  - Markets can be closed (no trading) but still useful for backtesting.
"""

import json
import urllib.request
import urllib.parse
import urllib.error
import time
from dataclasses import dataclass, field
from typing import Optional


GAMMA_HOST = "https://gamma-api.polymarket.com"


@dataclass
class MarketInfo:
    """Normalised representation of a single Polymarket market."""
    question: str
    slug: str
    condition_id: str
    clob_token_ids: list  # [yes_token, no_token]
    outcomes: list        # ["Yes", "No"]
    outcome_prices: list  # ["0.65", "0.35"] — current implied probs
    volume: float
    closed: bool
    active: bool
    description: str
    end_date: str
    market_id: str

    @property
    def yes_token(self) -> str:
        return self.clob_token_ids[0] if len(self.clob_token_ids) > 0 else ""

    @property
    def no_token(self) -> str:
        return self.clob_token_ids[1] if len(self.clob_token_ids) > 1 else ""

    @property
    def yes_price(self) -> float:
        return float(self.outcome_prices[0]) if self.outcome_prices else 0.0

    @property
    def no_price(self) -> float:
        return float(self.outcome_prices[1]) if len(self.outcome_prices) > 1 else 0.0


@dataclass
class EventInfo:
    """Normalised representation of a Polymarket event (group of markets)."""
    title: str
    slug: str
    event_id: str
    volume: float
    closed: bool
    active: bool
    description: str
    markets: list = field(default_factory=list)  # list[MarketInfo]


def _get(url: str, timeout: int = 15, retries: int = 3) -> dict | list:
    """GET with retry. Returns parsed JSON."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "quantmet-weather-trader/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.reason}"
            if e.code in (429, 503):
                time.sleep(2 ** attempt)  # backoff
                continue
            raise
        except urllib.error.URLError as e:
            last_err = f"Connection error: {e.reason}"
            time.sleep(2 ** attempt)
        except json.JSONDecodeError as e:
            last_err = f"JSON decode error: {e}"
            time.sleep(2 ** attempt)
    raise ConnectionError(f"Gamma API failed after {retries} retries: {last_err}")


def _parse_json_field(val):
    """Parse double-encoded JSON fields (outcomePrices, outcomes, clobTokenIds)."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


def _to_market_info(m: dict) -> MarketInfo:
    """Convert a raw Gamma market dict to MarketInfo."""
    tokens = _parse_json_field(m.get("clobTokenIds", "[]"))
    outcomes = _parse_json_field(m.get("outcomes", "[]"))
    prices = _parse_json_field(m.get("outcomePrices", "[]"))

    if not isinstance(tokens, list):
        tokens = []
    if not isinstance(outcomes, list):
        outcomes = []
    if not isinstance(prices, list):
        prices = []

    return MarketInfo(
        question=m.get("question", ""),
        slug=m.get("slug", ""),
        condition_id=m.get("conditionId", ""),
        clob_token_ids=tokens,
        outcomes=outcomes,
        outcome_prices=prices,
        volume=float(m.get("volume", 0) or 0),
        closed=bool(m.get("closed", False)),
        active=bool(m.get("active", True)),
        description=m.get("description", "") or "",
        end_date=m.get("endDate", "") or "",
        market_id=str(m.get("id", "")),
    )


def _to_event_info(e: dict) -> EventInfo:
    """Convert a raw Gamma event dict to EventInfo."""
    markets = [_to_market_info(m) for m in e.get("markets", [])]
    return EventInfo(
        title=e.get("title", ""),
        slug=e.get("slug", ""),
        event_id=str(e.get("id", "")),
        volume=float(e.get("volume", 0) or 0),
        closed=bool(e.get("closed", False)),
        active=bool(e.get("active", True)),
        description=e.get("description", "") or "",
        markets=markets,
    )


# --- Public API ---

def search_events(query: str, limit: int = 20) -> list[EventInfo]:
    """Search for events by keyword. Returns list of EventInfo."""
    q = urllib.parse.quote(query)
    data = _get(f"{GAMMA_HOST}/public-search?q={q}")
    events = data.get("events", []) if isinstance(data, dict) else []
    return [_to_event_info(e) for e in events[:limit]]


def get_event(slug: str) -> Optional[EventInfo]:
    """Fetch a single event by slug. Returns None if not found."""
    events = _get(f"{GAMMA_HOST}/events?slug={urllib.parse.quote(slug)}")
    if not events:
        return None
    return _to_event_info(events[0])


def get_market(slug: str) -> Optional[MarketInfo]:
    """Fetch a single market by slug. Returns None if not found."""
    markets = _get(f"{GAMMA_HOST}/markets?slug={urllib.parse.quote(slug)}")
    if not markets:
        return None
    return _to_market_info(markets[0])


def discover_weather_events(
    queries: list[str] = None,
    min_volume: float = 5000,
    active_only: bool = False,
) -> list[EventInfo]:
    """
    Discover weather-related events across multiple search queries.
    Deduplicates by event ID. Filters by min volume and optionally active status.

    NOTE: Returns ALL weather events (active and closed). The caller decides
    whether to trade (active) or backtest (closed). The episodic nature of
    weather markets means most will be closed at any given time.
    """
    if queries is None:
        queries = ["highest temperature", "snowfall", "rainfall", "wind speed", "humidity"]

    # Inject date-aware queries for today, tomorrow, and day-after.
    # /public-search caps at ~5 results per query, so generic terms like
    # "temperature" or "highest temperature" only return the nearest-day
    # markets. We need explicit "temperature july 8" style queries to
    # surface future-date markets.
    from datetime import datetime, timedelta
    today = datetime.now()
    date_queries = []
    for offset in range(3):  # today, tomorrow, day-after
        d = today + timedelta(days=offset)
        month_name = d.strftime("%B").lower()
        day = int(d.strftime("%d"))
        date_queries.extend([
            f"temperature {month_name} {day}",
            f"lowest temperature {month_name} {day}",
        ])
    # Prepend date-aware queries, then append the base list (deduped)
    all_queries = date_queries + [q for q in queries if q not in date_queries]

    seen_ids = set()
    results = []

    for q in all_queries:
        try:
            events = search_events(q, limit=50)
        except ConnectionError as e:
            # Log but continue — one query failing shouldn't kill discovery
            print(f"[WARN] search '{q}' failed: {e}")
            continue

        for evt in events:
            if evt.event_id in seen_ids:
                continue
            seen_ids.add(evt.event_id)

            if evt.volume < min_volume:
                continue
            if active_only and (evt.closed or not evt.active):
                continue

            results.append(evt)

    return results


def is_weather_event(evt: EventInfo) -> bool:
    """
    Heuristic: filter out false positives (e.g. SNOW ticker, Beast Games).
    A genuine weather event should mention temperature, snow, rainfall,
    or weather in the title or description.
    """
    text = (evt.title + " " + evt.description).lower()
    weather_terms = [
        "temperature", "°f", "°c", "fahrenheit", "celsius",
        "snow", "snowfall", "rainfall", "precipitation",
        "weather", "wind speed", "humidity",
    ]
    # Exclude obvious false positives
    false_positive_terms = ["earnings", "eps", "stock", "ticker", "senate", "primary"]
    if any(term in text for term in false_positive_terms):
        return False
    return any(term in text for term in weather_terms)


# --- Self-test ---

if __name__ == "__main__":
    print("Gamma Client Self-Test")
    print("=" * 60)

    # Test 1: fetch the known NYC April 16 market
    print("\n[Test 1] Fetch NYC April 16 temperature event...")
    evt = get_event("highest-temperature-in-nyc-on-april-16-2026")
    if evt:
        print(f"  Title: {evt.title}")
        print(f"  Volume: ${evt.volume:,.0f}")
        print(f"  Closed: {evt.closed}")
        print(f"  Markets: {len(evt.markets)}")
        print(f"  Description (first 200 chars): {evt.description[:200]}")
        # Show first 3 buckets
        for m in evt.markets[:3]:
            print(f"    - {m.question}")
            print(f"      tokens: yes={m.yes_token[:20]}... no={m.no_token[:20]}...")
            print(f"      prices: yes={m.yes_price} no={m.no_price}")
    else:
        print("  FAIL: event not found")

    # Test 2: discover weather events
    print("\n[Test 2] Discover weather events (all, including closed)...")
    events = discover_weather_events(min_volume=1000, active_only=False)
    weather_events = [e for e in events if is_weather_event(e)]
    print(f"  Found {len(events)} total events, {len(weather_events)} genuine weather")
    for e in weather_events[:5]:
        print(f"    [{('CLOSED' if e.closed else 'ACTIVE')}] {e.title}  vol=${e.volume:,.0f}  markets={len(e.markets)}")