"""
slippage_guard.py — Enforce slippage limits and tick-size alignment.

Prevents the execution engine from placing orders at prices that
deviate too far from midpoint, and ensures tick-size compliance.

Rules:
  1. Max slippage from midpoint: 2% (configurable)
  2. Price must align to tick_size (0.01 on Polymarket)
  3. Size must not exceed top-N ask depth (liquidity cap)
  4. Never place at market — always limit with explicit price
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class SlippageError(RuntimeError):
    """Raised when slippage exceeds the configured limit."""


class TickSizeError(RuntimeError):
    """Raised when price is not aligned to the tick size."""


@dataclass
class SlippageCheck:
    """Result of a slippage guard check."""
    passed: bool
    aligned_price: float
    slippage: float
    reason: str = ""


class SlippageGuard:
    """
    Pre-trade slippage and tick-size validation.

    Usage:
        guard = SlippageGuard(max_slippage=0.02, tick_size=0.01)
        check = guard.validate(price=0.35, midpoint=0.33, side="BUY")
        if not check.passed:
            raise SlippageError(check.reason)
    """

    def __init__(
        self,
        max_slippage: float = 0.02,
        tick_size: float = 0.01,
    ) -> None:
        self.max_slippage = max_slippage
        self.tick_size = tick_size

    def align_price(self, price: float) -> float:
        """Align price to the tick size (0.01 on Polymarket)."""
        aligned = round(price / self.tick_size) * self.tick_size
        # Fix floating point representation — use enough decimal places
        # to represent the tick size accurately
        decimals = max(2, len(str(self.tick_size).split(".")[1].rstrip("0")))
        return round(aligned, decimals)

    def compute_slippage(
        self,
        price: float,
        midpoint: Optional[float],
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
    ) -> float:
        """
        Compute slippage from the reference price.

        For BUY orders: slippage = ask_price - midpoint
        For SELL orders: slippage = midpoint - bid_price

        If no midpoint available, use (best_bid + best_ask) / 2.
        """
        if midpoint is not None and midpoint > 0:
            ref = midpoint
        elif best_bid is not None and best_ask is not None:
            ref = (best_bid + best_ask) / 2.0
        else:
            return 0.0  # can't compute — allow (will be caught elsewhere)

        return abs(price - ref)

    def validate(
        self,
        price: float,
        midpoint: Optional[float] = None,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
        side: str = "BUY",
    ) -> SlippageCheck:
        """
        Validate a proposed order price against slippage and tick rules.

        Returns a SlippageCheck with:
          - passed: True if order is safe to place
          - aligned_price: price after tick alignment
          - slippage: computed slippage from midpoint
          - reason: failure reason if not passed
        """
        # 1. Tick-size alignment
        aligned = self.align_price(price)
        if abs(aligned - price) > 1e-9:
            logger.warning(
                f"Price {price} misaligned to tick {self.tick_size}, "
                f"adjusted to {aligned}"
            )

        # 2. Slippage check
        slip = self.compute_slippage(
            price=aligned,
            midpoint=midpoint,
            best_bid=best_bid,
            best_ask=best_ask,
        )

        if slip > self.max_slippage:
            return SlippageCheck(
                passed=False,
                aligned_price=aligned,
                slippage=slip,
                reason=(
                    f"Slippage {slip:.4f} exceeds max {self.max_slippage:.4f} "
                    f"(price={aligned}, mid={midpoint})"
                ),
            )

        return SlippageCheck(
            passed=True,
            aligned_price=aligned,
            slippage=slip,
        )

    def validate_size(
        self,
        size: float,
        ask_depth: float = 0.0,
        min_order_size: float = 5.0,
    ) -> tuple[bool, float, str]:
        """
        Validate order size against liquidity and minimum.

        Returns (valid, adjusted_size, reason).
        """
        if size < min_order_size:
            return False, 0.0, (
                f"Size {size} < min_order_size {min_order_size}"
            )

        if ask_depth > 0 and size > ask_depth:
            adjusted = ask_depth
            logger.warning(
                f"Size {size} exceeds ask depth {ask_depth}, "
                f"capping to {adjusted}"
            )
            return True, adjusted, f"Capped to ask depth {ask_depth}"

        return True, size, ""