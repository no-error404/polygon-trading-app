"""
bankroll.py — On-chain pUSD balance tracking and reconciliation.

The bankroll is tracked from on-chain balance, not a local number,
because local state can drift from reality. We reconcile every cycle.

pUSD is Polymarket's V2 native collateral token (ERC-20 on Polygon).
Contract: 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB
This replaced the legacy bridged USDC.e after the April 2026 V2 upgrade.
The V2 ClobClient (py-clob-client-v2) targets this contract natively via
get_contract_config(chain_id).collateral.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BankrollState:
    """Snapshot of bankroll at a point in time."""
    total_balance: float          # on-chain pUSD/USDC balance
    available_balance: float      # balance minus open order reserves
    open_order_value: float       # total $ locked in open orders
    pending_settlement: float     # $ locked in closed markets awaiting oracle
    realised_pnl: float           # cumulative realised profit/loss
    unrealised_pnl: float         # mark-to-market on open positions
    high_water_mark: float        # peak bankroll (for drawdown tracking)
    timestamp: str = ""

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from high water mark (%)."""
        if self.high_water_mark <= 0:
            return 0.0
        return max(0.0, (self.high_water_mark - self.total_balance)
                   / self.high_water_mark * 100.0)

    @property
    def liquid_balance(self) -> float:
        """Balance that is actually deployable — excludes both open orders
        and capital locked in closed markets awaiting settlement."""
        return self.total_balance - self.open_order_value - self.pending_settlement


class Bankroll:
    """
    Tracks bankroll with on-chain reconciliation.

    Usage:
        bankroll = Bankroll(initial=1000.0)
        bankroll.update_from_chain(balance=985.0, open_orders=50.0)
        if bankroll.drawdown_pct > 8.0:
            # kill switch
    """

    def __init__(self, initial: float = 1000.0) -> None:
        self._state = BankrollState(
            total_balance=initial,
            available_balance=initial,
            open_order_value=0.0,
            pending_settlement=0.0,
            realised_pnl=0.0,
            unrealised_pnl=0.0,
            high_water_mark=initial,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @property
    def state(self) -> BankrollState:
        return self._state

    @property
    def total(self) -> float:
        return self._state.total_balance

    @property
    def available(self) -> float:
        return self._state.available_balance

    @property
    def liquid_balance(self) -> float:
        """Capital that is truly deployable — excludes open orders AND
        capital locked in closed markets awaiting oracle settlement."""
        return self._state.liquid_balance

    @property
    def pending_settlement(self) -> float:
        """Capital locked in closed markets awaiting oracle resolution."""
        return self._state.pending_settlement

    @property
    def drawdown_pct(self) -> float:
        return self._state.drawdown_pct

    def update_from_chain(
        self,
        balance: float,
        open_orders: float = 0.0,
    ) -> None:
        """
        Reconcile bankroll from on-chain data.

        Called every cycle to ensure local state matches reality.
        """
        prev = self._state.total_balance
        self._state.total_balance = balance
        self._state.open_order_value = open_orders
        self._state.available_balance = balance - open_orders

        # Track realised PnL change
        if prev > 0:
            delta = balance - prev - open_orders
            if abs(delta) > 0.01:
                self._state.realised_pnl += delta
                logger.info(
                    f"Bankroll reconciled: {prev:.2f} -> {balance:.2f} "
                    f"(delta={delta:+.2f})"
                )

        # Update high water mark
        if balance > self._state.high_water_mark:
            self._state.high_water_mark = balance

        self._state.timestamp = datetime.now(timezone.utc).isoformat()

    def reserve_for_order(self, order_value: float) -> bool:
        """
        Check if we have enough available balance for an order.
        Returns True if sufficient, False if not.
        """
        if order_value > self._state.available_balance:
            logger.warning(
                f"Insufficient balance: need ${order_value:.2f}, "
                f"available ${self._state.available_balance:.2f}"
            )
            return False
        self._state.available_balance -= order_value
        self._state.open_order_value += order_value
        return True

    def release_order_reserve(self, order_value: float) -> None:
        """Release reserved balance when order is cancelled."""
        self._state.available_balance += order_value
        self._state.open_order_value = max(
            0.0, self._state.open_order_value - order_value
        )

    def record_settlement(
        self, cost: float, payout: float
    ) -> None:
        """Record a settled position's PnL."""
        pnl = payout - cost
        self._state.realised_pnl += pnl
        self._state.available_balance += payout
        self._state.open_order_value = max(
            0.0, self._state.open_order_value - cost
        )
        logger.info(
            f"SETTLEMENT: cost=${cost:.2f} payout=${payout:.2f} "
            f"pnl={pnl:+.2f} total_pnl={self._state.realised_pnl:+.2f}"
        )

    def mark_pending_settlement(self, cost: float) -> None:
        """
        Move capital from open_order_value to pending_settlement.

        Called when a market closes (expires) but the oracle hasn't
        resolved yet. The tokens are still in our wallet but the market
        is locked — this capital is NOT liquid and must not be counted
        as available for new orders.

        This prevents the bot from over-allocating new capital thinking
        its bankroll is larger than what is actually liquid.
        """
        self._state.open_order_value = max(
            0.0, self._state.open_order_value - cost
        )
        self._state.pending_settlement += cost
        logger.info(
            f"PENDING SETTLEMENT: ${cost:.2f} moved to pending "
            f"(total pending=${self._state.pending_settlement:.2f}, "
            f"liquid=${self._state.liquid_balance:.2f})"
        )

    def resolve_settlement(self, cost: float, payout: float) -> None:
        """
        Resolve a pending settlement when the oracle pays out.

        Removes the capital from pending_settlement, records the PnL,
        and adds the payout back to available_balance.
        """
        pnl = payout - cost
        self._state.pending_settlement = max(
            0.0, self._state.pending_settlement - cost
        )
        self._state.realised_pnl += pnl
        self._state.available_balance += payout
        logger.info(
            f"SETTLEMENT RESOLVED: cost=${cost:.2f} payout=${payout:.2f} "
            f"pnl={pnl:+.2f} pending=${self._state.pending_settlement:.2f} "
            f"liquid=${self._state.liquid_balance:.2f}"
        )