"""
kill_switch.py — Emergency halt system for the trading bot.

Monitors multiple anomaly signals and triggers an emergency cancel-all
when any threshold is breached. This is the last line of defense before
money is lost to unexpected events.

Triggers:
  1. Drawdown > 8% in 24h
  2. 3 consecutive rejected orders
  3. Gamma/CLOB API unreachable for >2 cycles
  4. Weather data stream anomaly (NaN, missing, impossible values)
  5. Manual emergency stop

When triggered:
  - All open orders are batch-cancelled (fail-safe heartbeat)
  - No new orders are placed
  - Audit log records the trigger reason
  - Human intervention required to reset
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class KillSwitchTrigger(str, Enum):
    """What caused the kill switch to trip."""
    DRAWDOWN = "DRAWDOWN_EXCEEDED"
    REJECTED_ORDERS = "CONSECUTIVE_REJECTED_ORDERS"
    API_OUTAGE = "API_OUTAGE"
    WEATHER_ANOMALY = "WEATHER_DATA_ANOMALY"
    MANUAL = "MANUAL_STOP"
    EXPOSURE_CAP_BREACH = "EXPOSURE_CAP_BREACH"


@dataclass
class KillSwitchState:
    """Current state of the kill switch."""
    tripped: bool = False
    trigger: Optional[KillSwitchTrigger] = None
    tripped_at: str = ""
    consecutive_rejections: int = 0
    api_outage_cycles: int = 0
    last_drawdown_pct: float = 0.0
    details: str = ""


class KillSwitch:
    """
    Monitors trading conditions and can halt the system.

    Usage:
        ks = KillSwitch(max_drawdown_pct=8.0, max_rejections=3)
        if ks.check_drawdown(bankroll.drawdown_pct):
            await trader.cancel_all_orders()
        ks.check_api_health(reachable=True)
        if ks.is_tripped:
            # do not place new orders
    """

    def __init__(
        self,
        max_drawdown_24h_pct: float = 8.0,
        max_consecutive_rejected_orders: int = 3,
        max_api_outage_cycles: int = 2,
    ) -> None:
        self.max_drawdown = max_drawdown_24h_pct
        self.max_rejections = max_consecutive_rejected_orders
        self.max_outage_cycles = max_api_outage_cycles
        self._state = KillSwitchState()

    @property
    def state(self) -> KillSwitchState:
        return self._state

    @property
    def is_tripped(self) -> bool:
        return self._state.tripped

    @property
    def trigger(self) -> Optional[KillSwitchTrigger]:
        return self._state.trigger

    def trip(self, trigger: KillSwitchTrigger, details: str = "") -> None:
        """Trip the kill switch."""
        if self._state.tripped:
            return  # already tripped
        self._state.tripped = True
        self._state.trigger = trigger
        self._state.tripped_at = datetime.now(timezone.utc).isoformat()
        self._state.details = details
        logger.critical(
            f"KILL SWITCH TRIPPED: {trigger.value} — {details}"
        )

    def reset(self) -> None:
        """Reset the kill switch (requires human intervention)."""
        logger.warning("KILL SWITCH RESET — manual intervention")
        self._state = KillSwitchState()

    def check_drawdown(self, drawdown_pct: float) -> bool:
        """Check if drawdown exceeds threshold."""
        self._state.last_drawdown_pct = drawdown_pct
        if drawdown_pct >= self.max_drawdown:
            self.trip(
                KillSwitchTrigger.DRAWDOWN,
                f"Drawdown {drawdown_pct:.1f}% >= {self.max_drawdown:.1f}%",
            )
            return True
        return False

    def record_order_result(self, success: bool) -> bool:
        """
        Record order success/failure. Trips if consecutive rejections
        exceed the threshold.
        """
        if success:
            self._state.consecutive_rejections = 0
        else:
            self._state.consecutive_rejections += 1
            if self._state.consecutive_rejections >= self.max_rejections:
                self.trip(
                    KillSwitchTrigger.REJECTED_ORDERS,
                    f"{self._state.consecutive_rejections} consecutive "
                    f"rejected orders",
                )
                return True
        return False

    def check_api_health(self, reachable: bool) -> bool:
        """
        Record API reachability. Trips if unreachable for too many
        consecutive cycles.
        """
        if reachable:
            self._state.api_outage_cycles = 0
        else:
            self._state.api_outage_cycles += 1
            if self._state.api_outage_cycles >= self.max_outage_cycles:
                self.trip(
                    KillSwitchTrigger.API_OUTAGE,
                    f"API unreachable for {self._state.api_outage_cycles} "
                    f"consecutive cycles",
                )
                return True
        return False

    def check_weather_anomaly(
        self,
        temps: list[float],
        units: str = "F",
    ) -> bool:
        """
        Check for weather data anomalies that could indicate a bad
        forecast feed (NaN, impossible temps, empty ensemble).
        """
        if not temps:
            self.trip(
                KillSwitchTrigger.WEATHER_ANOMALY,
                "Empty weather ensemble — no forecast data",
            )
            return True

        for t in temps:
            if t is None or (isinstance(t, float) and t != t):  # NaN check
                self.trip(
                    KillSwitchTrigger.WEATHER_ANOMALY,
                    f"NaN in weather temps: {temps}",
                )
                return True

            # Impossible temperature ranges
            if units == "F" and (t < -100 or t > 200):
                self.trip(
                    KillSwitchTrigger.WEATHER_ANOMALY,
                    f"Impossible temp {t}°F in ensemble",
                )
                return True
            if units == "C" and (t < -80 or t > 100):
                self.trip(
                    KillSwitchTrigger.WEATHER_ANOMALY,
                    f"Impossible temp {t}°C in ensemble",
                )
                return True

        return False

    def manual_stop(self, reason: str = "manual") -> None:
        """Manual emergency stop."""
        self.trip(KillSwitchTrigger.MANUAL, reason)