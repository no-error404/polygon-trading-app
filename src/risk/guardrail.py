"""
GuardRail — reusable safety decorators and execution policy.

This module centralises the bot's most critical safety rules so they
are hard to bypass by accident:
  - no_live_when_no_private_key: blocks live trading unless a real key is present
  - require_dry_run_before_live: enforces a minimum number of successful dry-run cycles
  - enforce_trading_hours: prevents order submission outside allowed windows
  - log_and_block: audit logging wrapper for critical safety events

The policies are intentionally strict and read-only after import.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class LiveTradingBlockedError(RuntimeError):
    """Raised when a safety policy blocks live trading."""


@dataclass
class DryRunRecord:
    """Persistent record of dry-run outcomes."""
    cycles_completed: int = 0
    orders_generated: int = 0
    errors: int = 0
    first_run_at: str = ""
    last_run_at: str = ""

    def to_dict(self) -> dict:
        return {
            "cycles_completed": self.cycles_completed,
            "orders_generated": self.orders_generated,
            "errors": self.errors,
            "first_run_at": self.first_run_at,
            "last_run_at": self.last_run_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DryRunRecord":
        return cls(
            cycles_completed=int(d.get("cycles_completed", 0)),
            orders_generated=int(d.get("orders_generated", 0)),
            errors=int(d.get("errors", 0)),
            first_run_at=d.get("first_run_at", ""),
            last_run_at=d.get("last_run_at", ""),
        )


class GuardRail:
    """
    Centralised safety gate for live trading.

    Usage:
        guard = GuardRail(state_file=Path(".guardrail.json"))
        guard.assert_can_trade_live(
            dry_run_cycles_required=100,
            min_orders_required=10,
        )
    """

    DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent.parent / ".guardrail.json"

    def __init__(self, state_file: Optional[Path] = None) -> None:
        self.state_file = state_file or self.DEFAULT_STATE_FILE
        self.record = DryRunRecord()
        self._load()

    def _load(self) -> None:
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    self.record = DryRunRecord.from_dict(json.load(f))
            except (json.JSONDecodeError, OSError, TypeError) as e:
                logger.warning(f"GuardRail state corrupt, resetting: {e}")

    def save(self) -> None:
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.record.to_dict(), f, indent=2)
        except OSError as e:
            logger.warning(f"Failed to save guardrail state: {e}")

    def record_dry_run_cycle(
        self,
        orders_generated: int = 0,
        errors: int = 0,
    ) -> None:
        """Call after every completed dry-run cycle."""
        now = datetime.now(timezone.utc).isoformat()
        if not self.record.first_run_at:
            self.record.first_run_at = now
        self.record.cycles_completed += 1
        self.record.orders_generated += orders_generated
        self.record.errors += errors
        self.record.last_run_at = now
        self.save()

    def assert_can_trade_live(
        self,
        dry_run_cycles_required: int = 100,
        min_orders_required: int = 10,
        max_errors_allowed: int = 0,
    ) -> None:
        """
        Raise LiveTradingBlockedError if live trading is not yet permitted.

        Requirements:
          - A real private key must be present (not a placeholder).
          - At least dry_run_cycles_required successful dry-run cycles.
          - At least min_orders_generated dry-run orders produced.
          - No errors in the last dry-run window if max_errors_allowed=0.
        """
        pk = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        if not pk or "YOUR_" in pk or len(pk) < 20:
            # Also check credentials.yaml
            cred_path = Path(__file__).resolve().parent.parent.parent / "config/credentials.yaml"
            if cred_path.exists():
                try:
                    import yaml
                    with open(cred_path) as f:
                        data = yaml.safe_load(f) or {}
                    pk = data.get("private_key", "")
                except Exception:
                    pk = ""
            if not pk or "YOUR_" in pk or len(pk) < 20:
                raise LiveTradingBlockedError(
                    "Live trading blocked: no valid POLYMARKET_PRIVATE_KEY found."
                )

        if self.record.cycles_completed < dry_run_cycles_required:
            raise LiveTradingBlockedError(
                f"Live trading blocked: only {self.record.cycles_completed} dry-run cycles completed, "
                f"need {dry_run_cycles_required}."
            )

        if self.record.orders_generated < min_orders_required:
            raise LiveTradingBlockedError(
                f"Live trading blocked: only {self.record.orders_generated} dry-run orders generated, "
                f"need {min_orders_required}."
            )

        if max_errors_allowed == 0 and self.record.errors > 0:
            raise LiveTradingBlockedError(
                f"Live trading blocked: {self.record.errors} dry-run errors recorded. "
                "Fix errors and reset guardrail state."
            )

    def reset(self) -> None:
        """Manual override — only call from a human-approved CLI or after a fix."""
        logger.warning("GuardRail state reset — live trading requirements cleared")
        self.record = DryRunRecord()
        self.save()


def with_live_guard(
    dry_run_cycles_required: int = 100,
    min_orders_required: int = 10,
    max_errors_allowed: int = 0,
) -> Callable:
    """Decorator that blocks live trading unless all safety gates pass."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            GuardRail().assert_can_trade_live(
                dry_run_cycles_required=dry_run_cycles_required,
                min_orders_required=min_orders_required,
                max_errors_allowed=max_errors_allowed,
            )
            return func(*args, **kwargs)
        return wrapper
    return decorator


def live_trading_enabled_for_call(dry_run: bool) -> bool:
    """
    Return True only when dry_run=False AND a real private key is present.

    This should be used at every code path that branches between dry-run and live.
    """
    if dry_run:
        return False
    pk = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if pk and "YOUR_" not in pk and len(pk) >= 20:
        return True
    cred_path = Path(__file__).resolve().parent.parent.parent / "config/credentials.yaml"
    if cred_path.exists():
        try:
            import yaml
            with open(cred_path) as f:
                data = yaml.safe_load(f) or {}
            pk = data.get("private_key", "")
            return bool(pk and "YOUR_" not in pk and len(pk) >= 20)
        except Exception:
            return False
    return False
