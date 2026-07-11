"""
exposure.py — Aggregate position tracking with negative-risk awareness.

Ensures we never exceed:
  - max_single_market_fraction (5% of bankroll per contract)
  - max_total_exposure_fraction (25% of bankroll total)

NEGATIVE-RISK BRACKET MARKETS:
  Polymarket weather markets are structured as Negative Risk markets:
  multiple mutually-exclusive outcomes (brackets) under one master
  condition_id. Only one bucket can win. Buying NO across multiple
  buckets is a HEDGE, not additive risk.

  For example, if we buy NO on 5 buckets at $50 each ($250 total raw
  cost), our maximum possible loss is NOT $250. At most ONE of those
  NO bets can lose (the winning bucket). The other 4 NO bets all pay
  $1/share. So worst case: we lose $50 (one losing NO) and gain $200
  (four winning NOs) = net +$150. Maximum loss = $50, not $250.

  This module computes aggregate exposure using the MAXIMUM LOSS
  LAYOUT across mutually exclusive outcomes, not naive summation.

  For positions across DIFFERENT condition_ids (independent markets),
  exposure IS additive — those are separate events.

  For positions with the SAME condition_id (one bracket event):
    - Same-side positions (all NO or all YES): max_loss = max single
      position's cost (only one bucket wins, rest lose for YES; only
      one bucket loses, rest win for NO)
    - Mixed-side: computed per the worst outcome scenario
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """A single open position."""
    token_id: str
    event_slug: str
    bucket_label: str
    side: str          # "YES" or "NO"
    shares: float
    cost_basis: float  # total $ spent
    avg_price: float   # cost / shares
    current_value: float  # mark-to-market
    market_closed: bool = False      # True when market expired, awaiting oracle
    condition_id: str = ""           # Polymarket condition ID for settlement check

    @property
    def max_loss(self) -> float:
        """
        Maximum possible loss for this single position.

        For YES: max loss = cost_basis (if the bucket doesn't win, shares = $0)
        For NO:  max loss = cost_basis - shares * $1.00 (if the bucket DOES
                 win, NO pays $0; but we also lose the opportunity cost)
                 Actually: NO pays $0 if bucket wins, $1 if bucket loses.
                 So max_loss_NO = cost_basis (we spent $X, get $0 back).

        In both cases, max single-position loss = cost_basis.
        But in a bracket, only one bucket wins, so:
          - For YES: at most one YES wins (pays $1*shares), rest lose ($0).
            Max loss across all YES = total_cost - max_payout
            = sum(costs) - max(shares_i * $1) across buckets.
          - For NO: at most one NO loses (bucket wins, NO pays $0), rest
            win (pay $1*shares). Max loss = max(cost_i - 0) for the one
            that loses, minus the gains on the rest. But actually:
            total_payout = sum(shares_i * $1) for all losing buckets.
            The winning bucket's NO pays $0.
            So net = sum(payouts) - sum(costs) = sum(shares_i * 1) - total_cost.
            Worst case (one bucket wins): we lose cost of that one NO,
            gain everything else. Max loss = total_cost - sum(shares_j * 1
            for j != winning bucket).
            Since we don't know which bucket wins, worst case is the
            bucket where we have the MOST shares (NO loses on that one).
            max_loss = cost_winning - (sum(payouts_j) for j != winning)
            = total_cost - sum(shares_j * 1 for all j)
            + shares_winning * 1 (we don't get this back)
            Wait, simpler: for NO positions in one bracket:
              We pay total_cost. We get back shares * $1 for every bucket
              that DOESN'T win. The winning bucket pays $0 for NO.
              Worst case: the bucket where we have the MOST NO shares wins.
              max_loss = total_cost - (total_shares - max_shares) * $1
        """
        return self.cost_basis  # single position, pre-bracket logic


class ExposureManager:
    """
    Tracks aggregate exposure with negative-risk bracket awareness.

    Key distinction:
      - Positions in DIFFERENT condition_ids: exposure is ADDITIVE
        (independent events, both can lose)
      - Positions in the SAME condition_id: exposure is the MAXIMUM
        LOSS LAYOUT (mutually exclusive outcomes, only one wins)

    Pre-trade: check if a new position would exceed caps.
    Post-trade: record the position.
    On settlement: remove the position.
    """

    def __init__(
        self,
        bankroll: float = 1000.0,
        max_single_market_fraction: float = 0.05,
        max_total_exposure_fraction: float = 0.25,
    ) -> None:
        self.bankroll = bankroll
        self.max_single = max_single_market_fraction
        self.max_total = max_total_exposure_fraction
        self._positions: dict[str, Position] = {}  # token_id -> Position
        self._event_exposure: dict[str, float] = {}  # event_slug -> total $

    # --- Raw vs hedged exposure ---

    @property
    def total_exposure(self) -> float:
        """
        Total $ deployed in ALL positions (open + pending settlement).
        This is the RAW cost — does NOT account for negative-risk hedging.
        """
        return sum(p.cost_basis for p in self._positions.values())

    @property
    def active_raw_exposure(self) -> float:
        """
        Raw $ in active positions (naive sum, pre-hedging).
        Used as a ceiling — the hedged exposure is always <= this.
        """
        return sum(
            p.cost_basis for p in self._positions.values()
            if not p.market_closed
        )

    @property
    def active_exposure(self) -> float:
        """
        Hedged active exposure — accounts for negative-risk brackets.

        For positions sharing the same condition_id (one bracket event),
        computes the MAXIMUM LOSS LAYOUT instead of naive summation.

        For positions in different condition_ids (independent events),
        exposure is additive.

        This is the number used for exposure cap enforcement.
        """
        # Group active positions by condition_id
        groups: dict[str, list[Position]] = {}
        for pos in self._positions.values():
            if pos.market_closed:
                continue
            key = pos.condition_id or pos.token_id  # fallback: no condition = independent
            groups.setdefault(key, []).append(pos)

        total = 0.0
        for key, positions in groups.items():
            if len(positions) == 1:
                # Single position — no hedging possible
                total += positions[0].cost_basis
            else:
                # Multiple positions under same condition_id — negative risk
                total += self._compute_max_loss_layout(positions)

        return total

    def _compute_max_loss_layout(self, positions: list[Position]) -> float:
        """
        Compute the maximum possible loss for a group of positions
        sharing the same condition_id (mutually exclusive outcomes).

        For NO positions (buying NO across multiple buckets):
          - We pay total_cost for all NO tokens
          - At settlement, one bucket wins. NO on the winning bucket = $0.
            NO on all other buckets = $1/share.
          - Worst case: the bucket where we have the MOST NO shares wins.
            max_payout = (total_shares - max_shares) * $1.00
            max_loss = total_cost - max_payout

        For YES positions (buying YES across multiple buckets):
          - We pay total_cost for all YES tokens
          - At settlement, one bucket wins. YES on that bucket = $1/share.
            YES on all others = $0.
          - Worst case: the bucket where we have the FEWEST YES shares wins.
            max_payout = min_shares * $1.00
            max_loss = total_cost - max_payout

        For mixed YES+NO under same condition_id:
          - Compute per-bucket: if this bucket wins, YES pays $1*shares_yes,
            NO pays $0. If this bucket loses, YES pays $0, NO pays $1*shares_no.
          - Find the winning bucket that maximizes our loss.
        """
        # Separate by side
        yes_positions = [p for p in positions if p.side.upper() == "YES"]
        no_positions = [p for p in positions if p.side.upper() == "NO"]

        if not yes_positions and no_positions:
            # All NO — worst case: bucket with most NO shares wins
            total_cost = sum(p.cost_basis for p in no_positions)
            total_shares = sum(p.shares for p in no_positions)
            max_shares = max(p.shares for p in no_positions) if no_positions else 0
            max_payout = (total_shares - max_shares) * 1.0
            return max(0.0, total_cost - max_payout)

        elif yes_positions and not no_positions:
            # All YES — worst case: bucket with fewest YES shares wins
            total_cost = sum(p.cost_basis for p in yes_positions)
            min_shares = min(p.shares for p in yes_positions) if yes_positions else 0
            max_payout = min_shares * 1.0
            return max(0.0, total_cost - max_payout)

        else:
            # Mixed YES+NO — compute per-bucket worst case
            # For each bucket, if it wins: YES_payout = shares_yes * $1,
            # NO_payout = $0. If it loses: YES_payout = $0,
            # NO_payout = shares_no * $1.
            # We need to find which bucket winning gives us the worst outcome.

            # Build per-bucket payout maps
            # Group by bucket_label (each bucket may have both YES and NO)
            buckets: dict[str, dict] = {}
            for p in positions:
                buckets.setdefault(p.bucket_label, {
                    "yes_shares": 0.0, "no_shares": 0.0,
                    "yes_cost": 0.0, "no_cost": 0.0,
                })
                if p.side.upper() == "YES":
                    buckets[p.bucket_label]["yes_shares"] += p.shares
                    buckets[p.bucket_label]["yes_cost"] += p.cost_basis
                else:
                    buckets[p.bucket_label]["no_shares"] += p.shares
                    buckets[p.bucket_label]["no_cost"] += p.cost_basis

            total_cost = sum(p.cost_basis for p in positions)
            bucket_labels = list(buckets.keys())

            # For each possible winning bucket, compute our net payout
            worst_payout = float("inf")
            for winning_bucket in bucket_labels:
                payout = 0.0
                for label, data in buckets.items():
                    if label == winning_bucket:
                        # This bucket wins: YES pays $1/share, NO pays $0
                        payout += data["yes_shares"] * 1.0
                    else:
                        # This bucket loses: YES pays $0, NO pays $1/share
                        payout += data["no_shares"] * 1.0
                if payout < worst_payout:
                    worst_payout = payout

            return max(0.0, total_cost - worst_payout)

    @property
    def pending_settlement_value(self) -> float:
        """$ locked in closed markets awaiting oracle resolution."""
        return sum(
            p.cost_basis for p in self._positions.values()
            if p.market_closed
        )

    @property
    def total_exposure_pct(self) -> float:
        """Hedged active exposure as % of bankroll."""
        if self.bankroll <= 0:
            return 0.0
        return self.active_exposure / self.bankroll * 100.0

    @property
    def single_market_cap(self) -> float:
        return self.bankroll * self.max_single

    @property
    def total_exposure_cap(self) -> float:
        return self.bankroll * self.max_total

    @property
    def remaining_exposure(self) -> float:
        """
        How much more $ we can deploy before hitting the total cap.

        Uses hedged active_exposure (accounts for negative-risk brackets)
        so that hedged positions don't artificially consume cap headroom.
        """
        return max(0.0, self.total_exposure_cap - self.active_exposure)

    def check_caps(
        self,
        event_slug: str,
        proposed_bet: float,
        condition_id: str = "",
    ) -> tuple[bool, float, str]:
        """
        Check if a proposed bet fits within exposure caps.

        NEGATIVE-RISK AWARE:
          - If condition_id is provided, positions under the same
            condition_id are evaluated using max-loss layout, not
            naive summation. This prevents false cap exhaustion when
            buying NO across multiple mutually-exclusive brackets.
          - Both the single-market cap AND total cap use hedged exposure.

        Returns (allowed, max_allowed, reason).
        """
        # Single-market cap — use hedged exposure for this event
        # Group this event's active positions by condition_id and sum hedged
        event_positions = [
            p for p in self._positions.values()
            if p.event_slug == event_slug and not p.market_closed
        ]

        if condition_id and event_positions:
            # Compute hedged exposure for positions under the same condition_id
            same_cond = [p for p in event_positions
                         if p.condition_id == condition_id]
            other_cond = [p for p in event_positions
                          if p.condition_id != condition_id]

            # Hedged exposure for same condition_id group
            if len(same_cond) > 1:
                current_event = self._compute_max_loss_layout(same_cond)
            elif same_cond:
                current_event = same_cond[0].cost_basis
            else:
                current_event = 0.0

            # Add other condition_id positions (additive, independent)
            current_event += sum(p.cost_basis for p in other_cond)
        else:
            current_event = sum(p.cost_basis for p in event_positions)

        event_remaining = self.single_market_cap - current_event

        # Total exposure cap — uses hedged active exposure
        total_remaining = self.remaining_exposure

        # Binding constraint
        max_allowed = min(event_remaining, total_remaining)

        if max_allowed <= 0:
            return False, 0.0, "All exposure caps exhausted"

        if proposed_bet > max_allowed:
            return True, max_allowed, (
                f"Bet capped from ${proposed_bet:.2f} to ${max_allowed:.2f} "
                f"(event_cap={event_remaining:.2f}, "
                f"total_cap={total_remaining:.2f}, "
                f"hedged_exposure={self.active_exposure:.2f})"
            )

        return True, proposed_bet, "OK"

    def add_position(
        self,
        token_id: str,
        event_slug: str,
        bucket_label: str,
        side: str,
        shares: float,
        cost: float,
        condition_id: str = "",
    ) -> None:
        """Record a new position."""
        avg_price = cost / shares if shares > 0 else 0.0
        self._positions[token_id] = Position(
            token_id=token_id,
            event_slug=event_slug,
            bucket_label=bucket_label,
            side=side,
            shares=shares,
            cost_basis=cost,
            avg_price=avg_price,
            current_value=cost,
            condition_id=condition_id,
        )
        self._event_exposure[event_slug] = (
            self._event_exposure.get(event_slug, 0.0) + cost
        )
        logger.info(
            f"POSITION OPENED: {event_slug} {bucket_label} {side} "
            f"{shares:.1f} shares @ {avg_price:.3f} = ${cost:.2f} "
            f"condition={condition_id[:12] if condition_id else 'N/A'}"
        )

    def settle_position(self, token_id: str, payout: float) -> float:
        """Settle a position and return realised PnL."""
        if token_id not in self._positions:
            logger.warning(f"Settle unknown position: {token_id}")
            return 0.0

        pos = self._positions[token_id]
        pnl = payout - pos.cost_basis

        self._event_exposure[pos.event_slug] = max(
            0.0,
            self._event_exposure.get(pos.event_slug, 0.0) - pos.cost_basis
        )

        del self._positions[token_id]

        logger.info(
            f"POSITION SETTLED: {pos.event_slug} {pos.bucket_label} "
            f"cost=${pos.cost_basis:.2f} payout=${payout:.2f} "
            f"pnl={pnl:+.2f}"
        )
        return pnl

    def mark_market_closed(self, condition_id: str) -> int:
        """Mark all positions for a given condition_id as closed."""
        count = 0
        for pos in self._positions.values():
            if pos.condition_id == condition_id and not pos.market_closed:
                pos.market_closed = True
                self._event_exposure[pos.event_slug] = max(
                    0.0,
                    self._event_exposure.get(pos.event_slug, 0.0)
                    - pos.cost_basis
                )
                count += 1

        if count > 0:
            logger.info(
                f"MARKET CLOSED: {condition_id} — {count} positions "
                f"moved to pending settlement "
                f"(pending=${self.pending_settlement_value:.2f}, "
                f"active=${self.active_exposure:.2f})"
            )
        return count

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_active_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if not p.market_closed]

    def get_pending_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.market_closed]

    def get_event_exposure(self, event_slug: str) -> float:
        """Only counts active (non-closed) exposure for the event."""
        return sum(
            p.cost_basis for p in self._positions.values()
            if p.event_slug == event_slug and not p.market_closed
        )

    def get_condition_group_exposure(self, condition_id: str) -> float:
        """
        Get the hedged (max-loss) exposure for all positions under
        a single condition_id (one negative-risk bracket event).
        """
        positions = [
            p for p in self._positions.values()
            if p.condition_id == condition_id and not p.market_closed
        ]
        if not positions:
            return 0.0
        if len(positions) == 1:
            return positions[0].cost_basis
        return self._compute_max_loss_layout(positions)