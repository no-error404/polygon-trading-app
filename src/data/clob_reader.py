"""
CLOB Reader — read-only CLOB API queries (prices, orderbooks).

This module is READ-ONLY. It does NOT use authentication.
All authenticated operations go through clob_trader.py.

Uses the CLOB API at clob.polymarket.com for:
  - Current price (/price)
  - Midpoint (/midpoint)
  - Spread (/spread)
  - Orderbook depth (/book)
"""

import json
import urllib.request
import time
from dataclasses import dataclass, field


CLOB_HOST = "https://clob.polymarket.com"


@dataclass
class OrderBook:
    """Normalised orderbook for a single token."""
    token_id: str
    bids: list = field(default_factory=list)  # [{"price": "0.30", "size": "500"}, ...]
    asks: list = field(default_factory=list)  # sorted, best ask first
    tick_size: float = 0.01
    min_order_size: float = 5.0
    last_trade_price: float = 0.0

    @property
    def best_bid(self) -> float:
        return float(self.bids[0]["price"]) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return float(self.asks[0]["price"]) if self.asks else 1.0

    @property
    def mid(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def ask_depth_top3(self) -> float:
        """Total size of top 3 ask levels — used for liquidity cap."""
        return sum(float(a.get("size", 0)) for a in self.asks[:3])

    def available_depth_at_price(self, max_price: float) -> float:
        """
        Total shares available at or below max_price on the ask side.

        Walks down the ask ladder summing sizes while ask price <= max_price.
        Asks are sorted ascending (best/cheapest first), so we stop at the
        first level above max_price.
        """
        total = 0.0
        for a in self.asks:
            if float(a.get("price", 1)) > max_price:
                break
            total += float(a.get("size", 0))
        return total


def _get(url: str, timeout: int = 10, retries: int = 3) -> dict:
    """GET with retry. Returns parsed JSON."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "quantmet-weather-trader/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    raise ConnectionError(f"CLOB API failed after {retries} retries: {last_err}")


def get_price(token_id: str, side: str = "buy") -> float:
    """Get current price for a token. side='buy' or 'sell'."""
    data = _get(f"{CLOB_HOST}/price?token_id={token_id}&side={side}")
    return float(data.get("price", "0"))


def get_midpoint(token_id: str) -> float:
    """Get midpoint price for a token."""
    data = _get(f"{CLOB_HOST}/midpoint?token_id={token_id}")
    return float(data.get("mid", "0"))


def get_spread(token_id: str) -> float:
    """Get spread for a token."""
    data = _get(f"{CLOB_HOST}/spread?token_id={token_id}")
    return float(data.get("spread", "0"))


def get_orderbook(token_id: str) -> OrderBook:
    """Get full orderbook for a token."""
    data = _get(f"{CLOB_HOST}/book?token_id={token_id}")

    bids = data.get("bids", [])
    asks = data.get("asks", [])

    # Sort bids descending (best/higher first), asks ascending (best/lower first)
    bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
    asks = sorted(asks, key=lambda x: float(x.get("price", 1)))

    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        tick_size=float(data.get("tick_size", "0.01")),
        min_order_size=float(data.get("min_order_size", "5")),
        last_trade_price=float(data.get("last_trade_price", "0")),
    )


if __name__ == "__main__":
    print("CLOB Reader Self-Test")
    print("=" * 60)

    # Test with a known token from the NYC April 16 market
    # Use a sampling market instead since the NYC market is closed
    from src.data.gamma_client import get_event

    evt = get_event("highest-temperature-in-nyc-on-april-16-2026")
    if evt and evt.markets:
        m = evt.markets[0]
        print(f"\nMarket: {m.question}")
        print(f"Yes token: {m.yes_token[:30]}...")

        # Since it's closed, the orderbook may be empty — test anyway
        try:
            book = get_orderbook(m.yes_token)
            print(f"  Bids: {len(book.bids)}")
            print(f"  Asks: {len(book.asks)}")
            print(f"  Best bid: {book.best_bid}")
            print(f"  Best ask: {book.best_ask}")
            print(f"  Mid: {book.mid}")
            print(f"  Spread: {book.spread}")
            print(f"  Ask depth (top 3): {book.ask_depth_top3}")
            print(f"  Tick size: {book.tick_size}")
            print(f"  Min order size: {book.min_order_size}")
        except Exception as e:
            print(f"  Orderbook error (expected for closed market): {e}")

        try:
            mid = get_midpoint(m.yes_token)
            print(f"  Midpoint API: {mid}")
        except Exception as e:
            print(f"  Midpoint error: {e}")

    print("\n  Self-test complete.")