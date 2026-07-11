"""
Audit Log — append-only JSONL logger for every trading decision.

Every cycle of the soak test logs:
  - Timestamp
  - Market discovered
  - Station mapped
  - Forecast ensemble
  - EV calculations
  - Kelly sizing
  - Order placed (or skipped)
  - Balance

This is the forensic record. NEVER overwrite — append only.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    """Append-only JSONL audit logger."""

    def __init__(self, log_path: str = "logs/soak_audit.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file to ensure it exists
        if not self.log_path.exists():
            self.log_path.touch()

    def log(self, event: dict):
        """Append a JSON line to the audit log."""
        event["timestamp"] = datetime.now(timezone.utc).isoformat()
        with open(self.log_path, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def log_cycle(self, cycle_num: int, **kwargs):
        """Log a complete soak test cycle."""
        self.log({"type": "cycle", "cycle": cycle_num, **kwargs})

    def log_discovery(self, cycle_num: int, events_found: int, active_markets: list):
        """Log market discovery results."""
        self.log({
            "type": "discovery",
            "cycle": cycle_num,
            "events_found": events_found,
            "active_markets": [{"title": m.get("title", "?"), "slug": m.get("slug", "?")} for m in active_markets],
        })

    def log_forecast(self, cycle_num: int, station: str, target_date: str, max_temps: list, mean: float, stdev: float,
                     metric: str = "", market_slug: str = ""):
        """Log forecast ensemble results.

        Args:
            metric: "high" or "low" — disambiguates same station+date forecasts.
            market_slug: the Polymarket event slug for this market.
        """
        self.log({
            "type": "forecast",
            "cycle": cycle_num,
            "station": station,
            "target_date": target_date,
            "max_temps": [f"{t:.1f}" for t in max_temps],
            "mean": round(mean, 2),
            "stdev": round(stdev, 2),
            "metric": metric,
            "market_slug": market_slug,
        })

    def log_ev(self, cycle_num: int, bracket_label: str, p_model: float, p_market: float, ev: float, passes: bool,
               market_slug: str = "", station: str = ""):
        """Log EV calculation for a bracket."""
        self.log({
            "type": "ev",
            "cycle": cycle_num,
            "bracket": bracket_label,
            "p_model": round(p_model, 4),
            "p_market": round(p_market, 4),
            "ev_per_share": round(ev, 4),
            "passes_filter": passes,
            "market_slug": market_slug,
            "station": station,
        })

    def log_order(self, cycle_num: int, bracket_label: str, side: str, price: float, size: float, dry_run: bool,
                  order_id: str = "", status: str = "", error: str = "",
                  market_slug: str = "", station: str = ""):
        """Log an order placement (or dry-run)."""
        self.log({
            "type": "order",
            "cycle": cycle_num,
            "bracket": bracket_label,
            "side": side,
            "price": price,
            "size": size,
            "dry_run": dry_run,
            "order_id": order_id,
            "status": status,
            "error": error,
            "market_slug": market_slug,
            "station": station,
        })

    def log_balance(self, cycle_num: int, balance: float, n_open_orders: int):
        """Log current balance and open orders."""
        self.log({
            "type": "balance",
            "cycle": cycle_num,
            "balance_usdc": round(balance, 6),
            "open_orders": n_open_orders,
        })

    def log_error(self, cycle_num: int, error: str, context: str = ""):
        """Log an error."""
        self.log({
            "type": "error",
            "cycle": cycle_num,
            "error": error,
            "context": context,
        })

    def log_info(self, cycle_num: int, message: str):
        """Log an informational message."""
        self.log({
            "type": "info",
            "cycle": cycle_num,
            "message": message,
        })

    def read_recent(self, n: int = 10) -> list:
        """Read the last N log entries."""
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text().strip().split("\n")
        recent = lines[-n:] if len(lines) > n else lines
        return [json.loads(line) for line in recent if line.strip()]


if __name__ == "__main__":
    print("Audit Log Self-Test")
    print("=" * 60)

    log = AuditLog("logs/test_audit.jsonl")

    log.log_cycle(1, status="started")
    log.log_discovery(1, 3, [{"title": "NYC temp", "slug": "nyc-temp"}])
    log.log_forecast(1, "KLGA", "2026-07-16", [83.0, 86.0, 85.0], 84.67, 1.25,
                     metric="high", market_slug="highest-temperature-in-nyc-on-july-16-2026")
    log.log_ev(1, "86-87°F", 0.75, 0.30, 0.45, True)
    log.log_order(1, "86-87°F", "BUY", 0.30, 166.67, True, status="dry_run")
    log.log_balance(1, 49.00, 0)
    log.log_error(1, "test error", "testing error logging")
    log.log_info(1, "cycle complete")

    recent = log.read_recent(5)
    print(f"\n  Last 5 log entries:")
    for entry in recent:
        print(f"    [{entry.get('type', '?')}] {entry.get('timestamp', '?')[:19]}")

    # Clean up test log
    import os
    os.unlink("logs/test_audit.jsonl")
    print("\n  Self-test complete.")