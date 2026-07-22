"""
P&L Tracker — paper trading profit/loss analysis for dry-run orders.

Reads the soak audit log, reconstructs which market each dry-run order
belongs to, fetches resolution data from Gamma, and calculates what
would have happened if the orders were real.

Two modes:
  1. Retroactive: analyze past orders against resolved markets
  2. Ongoing: can be called by run.py to log P&L when markets resolve

Key logic:
  - Walk the audit log sequentially. Each forecast entry marks a new
    market block. All EV/order entries until the next forecast belong
    to that station+date.
  - Map (station, date) → event slug via station→city→slug pattern.
  - For resolved markets: winning bracket has yes_price=1.0, all others 0.0.
  - P&L per order: if bracket won → shares * $1 - cost. If lost → -cost.
  - For duplicate orders across cycles: take the LAST order per
    (station, date, bracket) before market resolution — that's the
    final position we would have held.
"""

from __future__ import annotations

import json
import re
import urllib.request
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml


# --- Station → City mapping (from stations.yaml) ---

def load_station_cities(stations_yaml_path: str) -> dict[str, str]:
    """Load ICAO → city name mapping from stations.yaml."""
    with open(stations_yaml_path) as f:
        data = yaml.safe_load(f)
    stations = data.get("stations", {})
    return {icao: info["city"] for icao, info in stations.items()}


# --- City → slug pattern ---

CITY_SLUG_MAP = {
    "New York City": "nyc",
    "Seoul": "seoul",
    "Shanghai": "shanghai",
    "Tokyo": "tokyo",
    "Shenzhen": "shenzhen",
    "Paris": "paris",
    "London": "london",
    "Hong Kong": "hong-kong",
    "Taipei": "taipei",
    "Wellington": "wellington",
    "Miami": "miami",
    "Chicago": "chicago",
    "Denver": "denver",
    "Los Angeles": "los-angeles",
    "Mount Washington": "mtpt-washington",
}


def station_date_to_slug(
    station_icao: str,
    target_date: str,
    station_cities: dict[str, str],
    event_title_hint: str = "",
    metric: str = "",
) -> Optional[str]:
    """
    Reconstruct the Polymarket event slug from station + date.

    Slug patterns:
      highest-temperature-in-{city}-on-{month}-{day}-{year}
      lowest-temperature-in-{city}-on-{month}-{day}-{year}

    Args:
        metric: "high" or "low" — takes priority over event_title_hint.
    """
    city = station_cities.get(station_icao)
    if not city:
        return None

    city_slug = CITY_SLUG_MAP.get(city, city.lower().replace(" ", "-"))

    # Parse target_date (YYYY-MM-DD)
    try:
        dt = datetime.fromisoformat(target_date)
    except (ValueError, TypeError):
        return None

    # Determine metric: explicit > title hint > default
    if metric:
        metric_slug = "lowest" if metric.lower().startswith("low") else "highest"
    elif event_title_hint:
        metric_slug = "lowest" if "lowest" in event_title_hint.lower() else "highest"
    else:
        metric_slug = "highest"

    # Build slug: highest-temperature-in-seoul-on-july-7-2026
    month_names = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]
    month = month_names[dt.month - 1]
    day = dt.day
    year = dt.year

    slug = f"{metric_slug}-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    return slug


def extract_date_from_slug(slug: str) -> str:
    """Extract YYYY-MM-DD date from a Polymarket event slug.

    Slug format: {metric}-temperature-in-{city}-on-{month}-{day}-{year}
    Returns empty string if parsing fails.
    """
    try:
        parts = slug.split("-")
        # Find the "on" separator, then month, day, year follow
        on_idx = parts.index("on")
        month_str = parts[on_idx + 1]
        day = int(parts[on_idx + 2])
        year = int(parts[on_idx + 3])
        month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]
        month = month_names.index(month_str) + 1
        return f"{year}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return ""


# --- Gamma API helper ---

GAMMA_HOST = "https://gamma-api.polymarket.com"


def _gamma_get(url: str, timeout: int = 15) -> dict | list:
    req = urllib.request.Request(
        url, headers={"User-Agent": "quantmet-pnl-tracker/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def get_resolved_event(slug: str) -> Optional[dict]:
    """
    Fetch event by slug. Returns raw Gamma event dict with markets.
    Returns None if not found.
    """
    try:
        events = _gamma_get(f"{GAMMA_HOST}/events?slug={urllib.parse.quote(slug)}")
        if events and len(events) > 0:
            return events[0]
    except Exception:
        pass
    return None


def get_winning_bracket(event: dict) -> Optional[str]:
    """
    Find the winning bracket question in a resolved event.
    Returns the question text of the winning market, or None if not resolved.
    """
    markets = event.get("markets", [])
    for m in markets:
        prices_raw = m.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw
        if prices and len(prices) > 0:
            try:
                yes_price = float(prices[0])
                if yes_price >= 0.99:
                    return m.get("question", "")
            except (ValueError, IndexError):
                continue
    return None


def is_event_resolved(event: dict) -> bool:
    """Check if an event is closed/resolved."""
    return bool(event.get("closed", False))


# --- Order grouping ---

@dataclass
class PaperOrder:
    """A single dry-run order reconstructed from the audit log."""
    cycle: int
    station: str
    target_date: str
    bracket: str
    side: str
    price: float
    size: float
    timestamp: str
    event_slug: str = ""
    event_title: str = ""


def reconstruct_orders(
    log_path: str,
    stations_yaml_path: str,
    since: str = "",
) -> list[PaperOrder]:
    """
    Walk the audit log and reconstruct orders with station+date context.

    Each forecast entry starts a new market block. All subsequent
    EV/order entries belong to that station+date until the next forecast.

    Args:
        log_path: Path to the soak_audit_dryrun.jsonl file.
        stations_yaml_path: Path to config/stations.yaml.
        since: Only include entries after this ISO timestamp.

    Returns:
        List of PaperOrder objects with station, date, and event_slug filled in.
    """
    station_cities = load_station_cities(stations_yaml_path)

    with open(log_path) as f:
        entries = [json.loads(line) for line in f if line.strip()]

    if since:
        entries = [e for e in entries if e.get("timestamp", "") >= since]

    orders: list[PaperOrder] = []
    current_station = None
    current_date = None
    current_metric = ""  # from forecast entries (new format)
    current_slug = ""    # from forecast entries (new format)
    current_title = ""  # from discovery entries

    # Build a title lookup from discovery entries
    # (to determine highest vs lowest temperature)
    title_by_slug = {}
    for e in entries:
        if e["type"] == "discovery":
            for m in e.get("active_markets", []):
                title_by_slug[m.get("slug", "")] = m.get("title", "")

    for e in entries:
        if e["type"] == "forecast":
            current_station = e["station"]
            current_date = e["target_date"]
            current_metric = e.get("metric", "")
            current_slug = e.get("market_slug", "")
            # Try to find the title from discovery
            slug = current_slug or station_date_to_slug(
                current_station, current_date, station_cities, "",
                metric=current_metric,
            )
            current_title = title_by_slug.get(slug, "")

        elif e["type"] == "order":
            # Prefer embedded market_slug/station (new format post-fix).
            # Fall back to reconstructed station+date (old log entries).
            embedded_slug = e.get("market_slug", "")
            embedded_station = e.get("station", "")

            if embedded_slug and embedded_station:
                # New format — use embedded fields directly
                orders.append(PaperOrder(
                    cycle=e["cycle"],
                    station=embedded_station,
                    target_date=extract_date_from_slug(embedded_slug),
                    bracket=e["bracket"],
                    side=e["side"],
                    price=e["price"],
                    size=e["size"],
                    timestamp=e["timestamp"],
                    event_slug=embedded_slug,
                    event_title="",
                ))
            elif current_station and current_date:
                # Old format — reconstruct from forecast context.
                # Use the metric from the forecast entry to build the correct slug.
                slug = embedded_slug or current_slug or station_date_to_slug(
                    current_station,
                    current_date,
                    station_cities,
                    current_title,
                    metric=current_metric,
                )
                orders.append(PaperOrder(
                    cycle=e["cycle"],
                    station=current_station,
                    target_date=current_date,
                    bracket=e["bracket"],
                    side=e["side"],
                    price=e["price"],
                    size=e["size"],
                    timestamp=e["timestamp"],
                    event_slug=slug or "",
                    event_title=current_title,
                ))

    return orders


# --- P&L calculation ---

@dataclass
class TradeResult:
    """P&L result for a single paper order."""
    station: str
    target_date: str
    event_slug: str
    bracket: str
    side: str
    price: float
    size: float
    cost: float           # price * size
    won: bool
    payout: float         # size * $1 if won, else $0
    pnl: float            # payout - cost
    timestamp: str
    winning_bracket: str  # the actual winning bracket question


@dataclass
class PnLReport:
    """Aggregated P&L report."""
    total_orders: int = 0
    resolved_orders: int = 0
    pending_orders: int = 0
    wins: int = 0
    losses: int = 0
    total_cost: float = 0.0
    total_payout: float = 0.0
    total_pnl: float = 0.0
    roi_pct: float = 0.0
    results: list[TradeResult] = field(default_factory=list)
    pending_markets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "PAPER TRADING P&L REPORT",
            "=" * 60,
            f"  Total dry-run orders: {self.total_orders}",
            f"  Resolved: {self.resolved_orders}  |  Pending: {self.pending_orders}",
            f"  Wins: {self.wins}  |  Losses: {self.losses}",
            f"  Win rate: {self.wins/max(1,self.resolved_orders)*100:.1f}%",
            f"  Total cost (would have spent): ${self.total_cost:.2f}",
            f"  Total payout (winnings): ${self.total_payout:.2f}",
            f"  Net P&L: ${self.total_pnl:+.2f}",
            f"  ROI: {self.roi_pct:+.1f}%",
        ]
        if self.pending_markets:
            lines.append(f"  Pending markets (not yet resolved):")
            for m in self.pending_markets[:5]:
                lines.append(f"    - {m}")
            if len(self.pending_markets) > 5:
                lines.append(f"    ... +{len(self.pending_markets)-5} more")
        if self.errors:
            lines.append(f"  Errors: {len(self.errors)}")
            for err in self.errors[:3]:
                lines.append(f"    - {err}")
        lines.append("=" * 60)
        return "\n".join(lines)


def calculate_pnl(
    orders: list[PaperOrder],
    cache: dict[str, dict] | None = None,
) -> PnLReport:
    """
    Calculate P&L for all paper orders.

    For each unique (station, date, bracket), takes the LAST order
    before market resolution — that's the final position we would hold.

    For resolved markets: fetches the winning bracket from Gamma and
    checks if our bracket matches.

    For unresolved markets: records as pending.
    """
    if cache is None:
        cache = {}

    report = PnLReport()
    report.total_orders = len(orders)

    # Deduplicate: keep last order per (event_slug, bracket)
    # (was (station, date, bracket) but new-format orders may lack date)
    last_orders: dict[tuple, PaperOrder] = {}
    for o in orders:
        key = (o.event_slug or o.station, o.bracket)
        last_orders[key] = o  # later orders overwrite earlier ones

    # Group by event_slug to fetch resolution once per market
    markets = defaultdict(list)
    for o in last_orders.values():
        group_key = o.event_slug or f"{o.station}_{o.target_date}"
        markets[group_key].append(o)

    for group_key, market_orders in sorted(markets.items()):
        slug = market_orders[0].event_slug
        station = market_orders[0].station
        date = market_orders[0].target_date
        if not slug:
            report.errors.append(f"No slug for {station} {date}")
            continue

        # Fetch event from cache or Gamma
        if slug not in cache:
            event = get_resolved_event(slug)
            if event is None:
                report.errors.append(f"Event not found: {slug}")
                continue
            cache[slug] = event
        event = cache[slug]

        if not is_event_resolved(event):
            report.pending_orders += len(market_orders)
            report.pending_markets.append(f"{slug} ({len(market_orders)} orders)")
            continue

        # Market is resolved — find winning bracket
        winning_q = get_winning_bracket(event)
        if not winning_q:
            report.errors.append(f"No winning bracket found for {slug}")
            continue

        # Match our bracket labels to the winning question
        # Our brackets: "30°C", ">=84°F", "<=22°C", "78-79°F"
        # Winning question: "Will the highest temperature in Seoul be 30°C or higher on July 7?"
        for o in market_orders:
            cost = o.price * o.size
            won = bracket_matches_question(o.bracket, winning_q)
            payout = o.size if won else 0.0
            pnl = payout - cost

            report.results.append(TradeResult(
                station=station,
                target_date=date,
                event_slug=slug,
                bracket=o.bracket,
                side=o.side,
                price=o.price,
                size=o.size,
                cost=cost,
                won=won,
                payout=payout,
                pnl=pnl,
                timestamp=o.timestamp,
                winning_bracket=winning_q,
            ))

            report.resolved_orders += 1
            report.total_cost += cost
            report.total_payout += payout
            report.total_pnl += pnl
            if won:
                report.wins += 1
            else:
                report.losses += 1

    if report.total_cost > 0:
        report.roi_pct = (report.total_pnl / report.total_cost) * 100

    return report


def bracket_matches_question(bracket_label: str, winning_question: str) -> bool:
    """
    Check if our bracket label matches the winning market question.

    Our labels: "30°C", ">=84°F", "<=22°C", "78-79°F"
    Winning Q:  "Will the highest temperature in Seoul be 30°C or higher on July 7?"

    Strategy: extract the numeric value and qualifier from both and compare.
    """
    # Parse our bracket label
    # Patterns: "30°C", ">=30°C", "<=22°C", "78-79°F", ">=84°F", "<=65°F"
    our_match = re.match(r'(>=|<=)?\s*(\d+)(?:-(\d+))?\s*°?([CF])', bracket_label)
    if not our_match:
        return False

    our_qual = our_match.group(1) or ""  # ">=", "<=", or "" (exact/range)
    our_low = int(our_match.group(2))
    our_high = int(our_match.group(3)) if our_match.group(3) else our_low
    our_unit = our_match.group(4)

    # Parse the winning question
    # "be 30°C or higher" → qual=">=", val=30
    # "be 30°C or below"  → qual="<=", val=30
    # "be between 78-79°F" → range 78-79
    # "be 30°C on" → exact 30
    win_match = re.search(
        r'be\s+(?:between\s+)?(\d+)(?:-(\d+))?\s*°?([CF])'
        r'(?:\s+or\s+(higher|below))?',
        winning_question, re.IGNORECASE
    )
    if not win_match:
        return False

    win_low = int(win_match.group(1))
    win_high = int(win_match.group(2)) if win_match.group(2) else win_low
    win_unit = win_match.group(3).upper()
    win_qual_word = win_match.group(4) or ""

    if win_qual_word.lower() == "higher":
        win_qual = ">="
        win_high = float('inf')
    elif win_qual_word.lower() == "below":
        win_qual = "<="
        win_low = float('-inf')
    else:
        win_qual = ""

    # Must match unit
    if our_unit != win_unit:
        return False

    # Match the bracket
    # Our label ">=30°C" should match winning "30°C or higher"
    # Our label "30°C" (exact) should match winning "30°C on" (exact)
    # Our label "78-79°F" should match winning "between 78-79°F"

    if our_qual and win_qual:
        # Both have qualifiers — compare directly
        if our_qual == win_qual:
            if our_qual == ">=":
                return our_low == win_low
            else:  # "<="
                return our_high == win_high
        return False

    if not our_qual and not win_qual:
        # Both are exact or range — compare values
        return our_low == win_low and our_high == win_high

    # Mixed: one has qualifier, other is exact/range
    # e.g. our "30°C" (exact, [30,31)) vs winning ">=30°C" — should match
    # if our range falls within the winning range
    if win_qual == ">=":
        return our_low >= win_low
    if win_qual == "<=":
        return our_high <= win_high
    if our_qual == ">=":
        return win_low >= our_low
    if our_qual == "<=":
        return win_high <= our_high

    return False


# --- Main entry point ---

def run_retroactive_analysis(
    log_path: str = "logs/soak_audit_dryrun.jsonl",
    stations_yaml_path: str = "config/stations.yaml",
    since: str = "",
) -> PnLReport:
    """
    Run full retroactive P&L analysis on the audit log.

    Args:
        log_path: Path to audit log JSONL.
        stations_yaml_path: Path to stations config.
        since: Only analyze entries after this ISO timestamp.

    Returns:
        PnLReport with results.
    """
    print("Reconstructing orders from audit log...")
    orders = reconstruct_orders(log_path, stations_yaml_path, since)
    print(f"  Found {len(orders)} dry-run orders")

    # Group by station+date for summary
    by_market = defaultdict(int)
    for o in orders:
        by_market[(o.station, o.target_date)] += 1
    print(f"  Across {len(by_market)} markets:")
    for (station, date), count in sorted(by_market.items()):
        print(f"    {station} {date}: {count} orders")

    print("\nFetching resolutions from Gamma API...")
    report = calculate_pnl(orders)
    print(report.summary())

    # Per-market breakdown
    if report.results:
        print("\n--- Per-market breakdown ---")
        by_market_results = defaultdict(list)
        for r in report.results:
            by_market_results[(r.station, r.target_date)].append(r)

        for (station, date), results in sorted(by_market_results.items()):
            market_cost = sum(r.cost for r in results)
            market_pnl = sum(r.pnl for r in results)
            wins = sum(1 for r in results if r.won)
            slug = results[0].event_slug
            winning = results[0].winning_bracket[:70]
            print(f"\n  {station} {date} ({slug})")
            print(f"    Winning bracket: {winning}")
            print(f"    Orders: {len(results)} ({wins} won, {len(results)-wins} lost)")
            print(f"    Cost: ${market_cost:.2f}  P&L: ${market_pnl:+.2f}")
            for r in results:
                marker = "WIN " if r.won else "LOSS"
                print(f"      {marker} {r.bracket:10s} @ ${r.price:.3f} x {r.size:.1f} = cost ${r.cost:.2f} pnl ${r.pnl:+.2f}")

    return report


if __name__ == "__main__":
    import sys
    project_root = Path(__file__).resolve().parent.parent.parent
    log = str(project_root / "logs" / "soak_audit_dryrun.jsonl")
    stations = str(project_root / "config" / "stations.yaml")

    since_arg = ""
    if len(sys.argv) > 1:
        since_arg = sys.argv[1]

    run_retroactive_analysis(log, stations, since_arg)