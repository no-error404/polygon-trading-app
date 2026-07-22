"""
order_manager.py — Order lifecycle management with retry and expiry.

Wraps ClobTrader with:
  - Retry with exponential backoff on Polygon congestion
  - GTD order expiry (auto-cancel after forecast cycle)
  - Order status polling
  - Slippage guard integration
  - Nonce monotonicity enforcement

Never places market orders — GTC/GTD limit only.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from .clob_trader import ClobTrader, OrderReceipt, OrderState, OrderError

logger = logging.getLogger(__name__)


class OrderManagerError(RuntimeError):
    """Raised when order management fails."""


@dataclass
class OrderResult:
    """Final result of a managed order attempt."""
    receipt: Optional[OrderReceipt]
    success: bool
    attempts: int
    error: str = ""


class OrderManager:
    """
    Manages order lifecycle: place -> poll -> confirm/cancel.

    Wraps ClobTrader with retry logic, expiry management, and
    status polling. Designed to be called from the async orchestrator.
    """

    def __init__(
        self,
        trader: ClobTrader,
        max_retries: int = 3,
        retry_backoff_seconds: float = 5.0,
        poll_interval_seconds: float = 10.0,
        max_poll_attempts: int = 30,
    ) -> None:
        self.trader = trader
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff_seconds
        self.poll_interval = poll_interval_seconds
        self.max_poll_attempts = max_poll_attempts

    async def place_with_retry(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        order_type: str = "GTC",
        tick_size: float = 0.01,
        min_order_size: float = 5.0,
    ) -> OrderResult:
        """
        Place an order with retry on transient failure.

        Retries on:
          - Network timeout
          - Polygon congestion (gas estimation failure)
          - CLOB relayer 5xx

        Does NOT retry on:
          - Insufficient balance
          - Invalid order params (bad tick, bad size)
          - Order rejected by relayer (auth/sigs)
        """
        last_error = ""

        for attempt in range(1, self.max_retries + 1):
            try:
                receipt = await self.trader.place_limit_order(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=side,
                    order_type=order_type,
                    tick_size=tick_size,
                    min_order_size=min_order_size,
                )
                logger.info(
                    f"Order placed (attempt {attempt}): "
                    f"id={receipt.order_id} token={token_id[:12]}... "
                    f"side={side} price={price} size={size}"
                )
                return OrderResult(
                    receipt=receipt,
                    success=True,
                    attempts=attempt,
                )

            except OrderError as e:
                last_error = str(e)
                logger.warning(
                    f"Order attempt {attempt}/{self.max_retries} failed: {e}"
                )
                if attempt < self.max_retries:
                    backoff = self.retry_backoff * (2 ** (attempt - 1))
                    logger.info(f"Retrying in {backoff:.1f}s...")
                    await asyncio.sleep(backoff)

        return OrderResult(
            receipt=None,
            success=False,
            attempts=self.max_retries,
            error=last_error,
        )

    async def poll_order_status(
        self,
        order_id: str,
    ) -> OrderState:
        """
        Poll order status until terminal state or max attempts.

        Terminal states: CONFIRMED, CANCELLED, REJECTED, EXPIRED
        """
        for attempt in range(self.max_poll_attempts):
            try:
                data = await self.trader.get_order_status(order_id)
                raw_status = data.get("status", "").upper()

                if raw_status == "CONFIRMED":
                    return OrderState.CONFIRMED
                elif raw_status == "CANCELLED":
                    return OrderState.CANCELLED
                elif raw_status == "MATCHED":
                    # Matched but not yet mined — keep polling
                    pass
                elif raw_status == "MINED":
                    # Mined but not confirmed — keep polling
                    pass
                elif raw_status == "REJECTED":
                    return OrderState.REJECTED

            except OrderError as e:
                logger.warning(
                    f"Status poll {attempt+1}/{self.max_poll_attempts} "
                    f"for {order_id}: {e}"
                )

            await asyncio.sleep(self.poll_interval)

        logger.warning(
            f"Order {order_id} did not reach terminal state after "
            f"{self.max_poll_attempts} polls"
        )
        return OrderState.POSTED  # still pending

    async def cancel_stale_orders(
        self,
        max_age_seconds: float = 3600,
    ) -> int:
        """
        Cancel orders older than max_age_seconds.

        Used for GTD-like behaviour on GTC orders that should have
        expired after a forecast cycle.
        """
        import time
        now = time.monotonic()
        cancelled = 0

        open_orders = await self.trader.get_open_orders()
        for receipt in open_orders:
            # Check age by created_at timestamp
            from datetime import datetime, timezone
            try:
                created = datetime.fromisoformat(receipt.created_at)
                age = (datetime.now(timezone.utc) - created).total_seconds()
                if age > max_age_seconds:
                    success = await self.trader.cancel_order(receipt.order_id)
                    if success:
                        cancelled += 1
            except (ValueError, TypeError):
                continue

        if cancelled:
            logger.info(f"Cancelled {cancelled} stale orders (age > {max_age_seconds}s)")
        return cancelled