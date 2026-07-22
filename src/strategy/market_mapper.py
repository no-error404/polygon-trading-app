"""
Market Mapper — parse Polymarket weather bracket markets into structured data.

Extracts:
  - Temperature brackets from market questions (e.g. "78-79°F")
  - Unit detection (°F vs °C)
  - Normalised book (implied probabilities summing to 1.0)
  - Market-to-station mapping via station_lookup
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Bracket:
    """A single temperature bracket in a weather market."""
    label: str          # e.g. "78-79°F" or "86-87" or ">=96°F"
    low: float          # lower bound (inclusive), or -inf for "<=" brackets
    high: float         # upper bound (inclusive), or +inf for ">=" brackets
    inclusive_low: bool = True
    inclusive_high: bool = True
    market_index: int = 0  # index in the event's markets list
    question: str = ""
    condition_id: str = ""
    yes_token: str = ""
    no_token: str = ""
    yes_price: float = 0.0
    no_price: float = 0.0

    @property
    def implied_prob(self) -> float:
        """Market's implied probability for this bracket."""
        return self.yes_price

    def contains(self, temp: float) -> bool:
        """Check if a temperature falls in this bracket."""
        if self.inclusive_low and temp < self.low:
            return False
        if not self.inclusive_low and temp <= self.low:
            return False
        if self.inclusive_high and temp > self.high:
            return False
        if not self.inclusive_high and temp >= self.high:
            return False
        return True


@dataclass
class WeatherMarket:
    """A complete weather bracket market (event with all brackets)."""
    title: str
    slug: str
    station_icao: str
    units: str  # "F" or "C"
    target_date: str  # YYYY-MM-DD or empty
    metric: str = "high"  # "high" (max temp) or "low" (min temp)
    brackets: list = field(default_factory=list)  # list[Bracket]
    total_implied_prob: float = 1.0  # sum of all bracket prices (may be >1 or <1)

    def normalise(self):
        """Normalise bracket implied probs so they sum to 1.0."""
        total = sum(b.yes_price for b in self.brackets)
        if total > 0:
            self.total_implied_prob = total
            for b in self.brackets:
                b.yes_price = b.yes_price / total

    def find_bracket(self, temp: float) -> Optional[Bracket]:
        """Find which bracket a temperature falls into."""
        for b in self.brackets:
            if b.contains(temp):
                return b
        return None


def parse_bracket(question: str, market_index: int = 0) -> Optional[Bracket]:
    """
    Parse a temperature bracket from a market question.

    Handles patterns like:
      "Will the highest temperature in New York City be 77°F or below on April 16?"
      "Will the highest temperature in New York City be between 78-79°F on April 16?"
      "Will the highest temperature in New York City be 96°F or higher on April 16?"
      "Will the highest temperature in Seoul be 14°C on April 17?"  (exact, 1-degree)
    """
    # Pattern: "X°F or below" → (-inf, X]
    m = re.search(r'(\d+)\s*°?F?\s*(?:or\s*)?below', question, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        return Bracket(
            label=f"<={int(val)}°F",
            low=float('-inf'),
            high=val,
            inclusive_high=True,
            market_index=market_index,
            question=question,
        )

    # Pattern: "X°C or below"
    m = re.search(r'(\d+)\s*°?C?\s*(?:or\s*)?below', question, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        return Bracket(
            label=f"<={int(val)}°C",
            low=float('-inf'),
            high=val,
            inclusive_high=True,
            market_index=market_index,
            question=question,
        )

    # Pattern: "X°F or higher" → [X, +inf)
    m = re.search(r'(\d+)\s*°?F?\s*(?:or\s*)?higher', question, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        return Bracket(
            label=f">={int(val)}°F",
            low=val,
            high=float('inf'),
            inclusive_low=True,
            market_index=market_index,
            question=question,
        )

    # Pattern: "X°C or higher"
    m = re.search(r'(\d+)\s*°?C?\s*(?:or\s*)?higher', question, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        return Bracket(
            label=f">={int(val)}°C",
            low=val,
            high=float('inf'),
            inclusive_low=True,
            market_index=market_index,
            question=question,
        )

    # Pattern: "between X-Y°F" → [X, Y] (inclusive both)
    m = re.search(r'between\s+(\d+)-(\d+)\s*°?F', question, re.IGNORECASE)
    if m:
        low = float(m.group(1))
        high = float(m.group(2))
        return Bracket(
            label=f"{int(low)}-{int(high)}°F",
            low=low,
            high=high,
            inclusive_low=True,
            inclusive_high=True,
            market_index=market_index,
            question=question,
        )

    # Pattern: "between X-Y°C"
    m = re.search(r'between\s+(\d+)-(\d+)\s*°?C', question, re.IGNORECASE)
    if m:
        low = float(m.group(1))
        high = float(m.group(2))
        return Bracket(
            label=f"{int(low)}-{int(high)}°C",
            low=low,
            high=high,
            inclusive_low=True,
            inclusive_high=True,
            market_index=market_index,
            question=question,
        )

    # Pattern: "be X°C on" (exact, 1-degree Celsius bracket) → [X, X+1)
    m = re.search(r'be\s+(\d+)\s*°?C\s+(?:on|in)', question, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        return Bracket(
            label=f"{int(val)}°C",
            low=val,
            high=val + 1,  # 1-degree bracket
            inclusive_low=True,
            inclusive_high=False,  # [X, X+1) — next bracket starts at X+1
            market_index=market_index,
            question=question,
        )

    # Pattern: "be X°F on" (exact, 1-degree Fahrenheit) — rare but handle
    m = re.search(r'be\s+(\d+)\s*°?F\s+(?:on|in)', question, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        return Bracket(
            label=f"{int(val)}°F",
            low=val,
            high=val + 1,
            inclusive_low=True,
            inclusive_high=False,
            market_index=market_index,
            question=question,
        )

    return None


def detect_units(question: str, market_questions: list = None) -> str:
    """
    Detect whether the market uses Fahrenheit or Celsius.

    Checks the event title first, then falls back to the individual
    market questions (which contain the actual unit like "22°C" or "78°F").
    Event titles like "Highest temperature in Seoul on July 7?" don't
    contain the unit — only the bracket questions do.
    """
    text = question
    if market_questions:
        text += " " + " ".join(market_questions[:3])

    if "°F" in text or "Fahrenheit" in text:
        return "F"
    if "°C" in text or "Celsius" in text:
        return "C"
    # Default: US cities use F, international use C
    return "F"


def map_weather_market(event, station_lookup) -> Optional[WeatherMarket]:
    """
    Map a Polymarket EventInfo to a WeatherMarket with brackets and station.

    Returns None if:
      - No station found in the description
      - No brackets parsed from questions
    """
    # Parse station from description
    station = station_lookup.parse_from_description(event.description)
    if station is None:
        return None

    # Detect units (pass market questions — titles often lack the unit)
    units = detect_units(event.title, [m.question for m in event.markets])

    # Detect metric: "highest" → max temp, "lowest" → min temp
    metric = "low" if "lowest" in event.title.lower() else "high"

    # Parse all brackets
    brackets = []
    for i, m in enumerate(event.markets):
        bracket = parse_bracket(m.question, market_index=i)
        if bracket is None:
            continue
        bracket.condition_id = m.condition_id
        bracket.yes_token = m.yes_token
        bracket.no_token = m.no_token
        bracket.yes_price = m.yes_price
        bracket.no_price = m.no_price
        brackets.append(bracket)

    if not brackets:
        return None

    # Sort brackets by low bound
    brackets.sort(key=lambda b: b.low)

    # Extract target date from title (e.g. "Highest temperature in Seoul on July 8?")
    # Format: "on July 8" → "2026-07-08" (assume current year)
    target_date = ""
    date_match = re.search(r"on\s+(\w+)\s+(\d{1,2})(?:\s+'?(\d{2,4}))?", event.title)
    if date_match:
        month_str, day_str, year_str = date_match.group(1), date_match.group(2), date_match.group(3)
        from datetime import datetime as _dt
        months = ["January","February","March","April","May","June",
                  "July","August","September","October","November","December"]
        month_idx = None
        for i, m in enumerate(months, 1):
            if m.lower() == month_str.lower():
                month_idx = i
                break
        if month_idx:
            year = int(year_str) if year_str else _dt.now().year
            if year < 100:
                year += 2000
            target_date = f"{year:04d}-{month_idx:02d}-{int(day_str):02d}"

    market = WeatherMarket(
        title=event.title,
        slug=event.slug,
        station_icao=station.icao,
        units=units,
        target_date=target_date,
        metric=metric,
        brackets=brackets,
    )

    # Normalise implied probs
    market.normalise()

    return market


if __name__ == "__main__":
    print("Market Mapper Self-Test")
    print("=" * 60)

    # Test bracket parsing
    test_questions = [
        ("Will the highest temperature in New York City be 77°F or below on April 16?", "<=77"),
        ("Will the highest temperature in New York City be between 78-79°F on April 16?", "78-79"),
        ("Will the highest temperature in New York City be 96°F or higher on April 16?", ">=96"),
        ("Will the highest temperature in Seoul be 14°C on April 17?", "14°C"),
        ("Will the highest temperature in Seoul be 8°C or below on April 17?", "<=8"),
    ]

    for q, expected in test_questions:
        b = parse_bracket(q, 0)
        if b:
            status = "PASS" if expected in b.label else "FAIL"
            print(f"  [{status}] '{q[:60]}...' → {b.label} (low={b.low}, high={b.high})")
        else:
            print(f"  [FAIL] '{q[:60]}...' → None")

    # Test unit detection
    print()
    tests = [
        ("...77°F or below...", "F"),
        ("...14°C on April...", "C"),
        ("...Fahrenheit on...", "F"),
        ("...Celsius on...", "C"),
    ]
    for q, expected in tests:
        result = detect_units(q)
        status = "PASS" if result == expected else "FAIL"
        print(f"  [{status}] '{q}' → {result}")

    # Test full market mapping with a real event
    print()
    print("--- Full market mapping test ---")
    from src.data.gamma_client import get_event
    from src.data.station_lookup import StationLookup

    lookup = StationLookup("config/stations.yaml")
    evt = get_event("highest-temperature-in-nyc-on-april-16-2026")
    if evt:
        market = map_weather_market(evt, lookup)
        if market:
            print(f"  Title: {market.title}")
            print(f"  Station: {market.station_icao}")
            print(f"  Units: {market.units}")
            print(f"  Brackets: {len(market.brackets)}")
            print(f"  Total implied prob: {market.total_implied_prob:.4f}")
            for b in market.brackets:
                print(f"    {b.label}: price={b.yes_price:.4f} token={b.yes_token[:20]}...")
        else:
            print("  FAIL: could not map market")