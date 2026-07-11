"""
Position Manager — track open positions and sell profitable ones.

Tracks all positions bought by the bot, monitors current market prices,
and places SELL orders when unrealized PnL exceeds a threshold.

This uses the EXISTING CLOB infrastructure (ClobTrader.place_limit_order)
which already works through the Polymarket proxy/relayer. No MATIC needed.

For resolved markets, positions are flagged for redemption by the redeemer.
"""

import json
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """A single open position held by the bot."""
    condition_id: str
    token_id: str          # CLOB token ID (YES side)
    market_slug: str
    station: str
    bracket: str           # e.g. "34°C"
    outcome: str           # "Yes", "No", etc.
    size: float            # number of shares
    avg_price: float       # average buy price
    cost_basis: float      # total USD spent
    trades: list = field(default_factory=list)  # raw trade dicts
    resolved: bool = False
    won: Optional[bool] = None  # True=won, False=lost, None=unresolved

    @property
    def current_value(self) -> float:
        """Current market value (size * current_price). Override with live price."""
        return 0.0

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.cost_basis <= 0:
            return 0.0
        return (self.current_value - self.cost_basis) / self.cost_basis


class PositionManager:
    """
    Manages open positions: tracks them from trade history, monitors prices,
    and sells profitable ones through the CLOB.

    Integration: called at the START of each sniper cycle, before discovery.
    """

    def __init__(self, settings: dict, audit=None):
        self.settings = settings
        self.audit = audit
        self.min_sell_pnl_pct = settings.get("trading", {}).get("min_sell_pnl_pct", 0.30)
        self.min_sell_size = settings.get("trading", {}).get("min_sell_size", 5.0)

    def load_positions(self) -> list[Position]:
        """Load all open positions from CLOB trade history.

        Queries get_trades_paginated(), groups by token, computes net size.
        Only returns positions with net_size > 0 (i.e. we still hold shares).
        """
        import yaml
        from pathlib import Path

        # Load credentials silently
        cred_path = Path("config/credentials.yaml")
        with open(cred_path) as f:
            creds = yaml.safe_load(f)

        deposit_wallet = self.settings.get("trading", {}).get(
            "deposit_wallet", "0x157F0453492B326DC3E995DC3C898768184Ee6d9"
        )

        from py_clob_client_v2.client import ClobClient as ClobClientV2
        from py_clob_client_v2.clob_types import ApiCreds as ApiCredsV2

        creds_obj = ApiCredsV2(
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            api_passphrase=creds["api_passphrase"],
        )
        client = ClobClientV2(
            host="https://clob.polymarket.com",
            key=creds["private_key"],
            chain_id=137,
            creds=creds_obj,
            signature_type=3,
            funder=deposit_wallet,
        )

        # Get all trades
        resp = client.get_trades_paginated()
        all_trades = resp.get("trades", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])

        # Group by condition_id + asset_id
        grouped = {}
        for t in all_trades:
            cid = t.get("market", "")
            aid = t.get("asset_id", "")
            side = t.get("side", "")
            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
            outcome = t.get("outcome", "?")

            key = f"{cid}:{aid}"
            if key not in grouped:
                grouped[key] = {
                    "condition_id": cid,
                    "token_id": aid,
                    "outcome": outcome,
                    "net_size": 0.0,
                    "total_cost": 0.0,
                    "buy_count": 0,
                    "sell_count": 0,
                    "trades": [],
                }
            if side == "BUY":
                grouped[key]["net_size"] += size
                grouped[key]["total_cost"] += size * price
                grouped[key]["buy_count"] += 1
            else:
                grouped[key]["net_size"] -= size
                grouped[key]["total_cost"] -= size * price
                grouped[key]["sell_count"] += 1
            grouped[key]["trades"].append(t)

        # Build Position objects for non-zero positions
        positions = []
        for key, g in grouped.items():
            if g["net_size"] <= 0.01:
                continue  # fully sold or short
            avg_price = g["total_cost"] / g["net_size"] if g["net_size"] > 0 else 0
            pos = Position(
                condition_id=g["condition_id"],
                token_id=g["token_id"],
                market_slug="",  # filled later from Gamma
                station="",
                bracket="",
                outcome=g["outcome"],
                size=g["net_size"],
                avg_price=avg_price,
                cost_basis=g["total_cost"],
                trades=g["trades"],
            )
            positions.append(pos)

        return positions

    def _get_clob_client(self):
        """Build a CLOB client for market queries (cached)."""
        if hasattr(self, "_clob_client"):
            return self._clob_client
        import yaml
        from pathlib import Path
        cred_path = Path("config/credentials.yaml")
        with open(cred_path) as f:
            creds = yaml.safe_load(f)
        deposit_wallet = self.settings.get("trading", {}).get(
            "deposit_wallet", "0x157F0453492B326DC3E995DC3C898768184Ee6d9"
        )
        from py_clob_client_v2.client import ClobClient as ClobClientV2
        from py_clob_client_v2.clob_types import ApiCreds as ApiCredsV2
        creds_obj = ApiCredsV2(
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            api_passphrase=creds["api_passphrase"],
        )
        self._clob_client = ClobClientV2(
            host="https://clob.polymarket.com",
            key=creds["private_key"],
            chain_id=137,
            creds=creds_obj,
            signature_type=3,
            funder=deposit_wallet,
        )
        return self._clob_client

    def enrich_positions(self, positions: list[Position]) -> list[Position]:
        """Fill in market_slug, station, bracket, and current price.

        Uses CLOB get_market(condition_id) — the Gamma API conditionID param
        returns garbage data (all condition IDs map to wrong markets).
        Falls back to Gamma events?slug= if CLOB fails.
        """
        import requests

        client = self._get_clob_client()

        for pos in positions:
            try:
                # Primary: CLOB get_market by condition ID
                m = client.get_market(pos.condition_id)
                if not m:
                    continue

                pos.market_slug = m.get("slug", m.get("market_slug", ""))
                pos.resolved = bool(m.get("closed", False))

                # Get current prices from CLOB orderbook for our token
                try:
                    from src.data.clob_reader import get_orderbook
                    book = get_orderbook(pos.token_id)
                    if book:
                        pos._current_price = book.best_bid  # what we'd get selling
                    else:
                        pos._current_price = 0.0
                except Exception:
                    pos._current_price = 0.0

                # Extract station from slug
                slug = pos.market_slug.lower()
                station_map = {
                    "paris": "LFPB", "nyc": "KLGA", "new-york": "KLGA",
                    "tokyo": "RJTT", "seoul": "RKSI", "shanghai": "ZSPD",
                    "taipei": "RCSS", "london": "EGLC", "wellington": "NZWN",
                }
                for key, icao in station_map.items():
                    if key in slug:
                        pos.station = icao
                        break

                # Extract bracket from question
                q = m.get("question", m.get("title", ""))
                pos.bracket = q

            except Exception as e:
                # Fallback: try Gamma events API by slug (if we have one)
                logger.warning(f"CLOB enrichment failed for {pos.condition_id[:16]}...: {e}")
                try:
                    if pos.market_slug:
                        r = requests.get(
                            "https://gamma-api.polymarket.com/events",
                            params={"slug": pos.market_slug},
                            timeout=15,
                        )
                        data = r.json()
                        if data:
                            evt = data[0]
                            for mk in evt.get("markets", []):
                                if mk.get("conditionId", "") == pos.condition_id:
                                    pos.resolved = bool(mk.get("closed", False))
                                    q = mk.get("question", "")
                                    pos.bracket = q
                                    break
                except Exception as e2:
                    logger.warning(f"Gamma fallback also failed: {e2}")

        return positions

    def get_current_price(self, token_id: str) -> float:
        """Get current best bid for a token from the CLOB orderbook."""
        try:
            from src.data.clob_reader import get_orderbook
            book = get_orderbook(token_id)
            return book.best_bid if book else 0.0
        except Exception:
            return 0.0

    def should_sell(self, pos: Position, current_price: float) -> tuple[bool, str]:
        """Determine if a position should be sold.

        Returns (should_sell, reason).
        """
        if pos.resolved:
            return False, "market resolved — handled by redeemer"

        if current_price <= 0:
            return False, "no bid (market not pricing this bracket)"

        pnl_pct = (current_price - pos.avg_price) / pos.avg_price if pos.avg_price > 0 else 0

        if pos.size < self.min_sell_size:
            return False, f"size {pos.size:.2f} < min {self.min_sell_size}"

        if pnl_pct >= self.min_sell_pnl_pct:
            return True, f"profit taking: pnl={pnl_pct:+.1%} (threshold={self.min_sell_pnl_pct:.0%})"

        # Stop loss: if price dropped >80%, cut losses
        if pnl_pct <= -0.80:
            return True, f"stop loss: pnl={pnl_pct:+.1%}"

        return False, f"holding: pnl={pnl_pct:+.1%} (threshold={self.min_sell_pnl_pct:.0%})"

    def sell_position(self, pos: Position, sell_price: float) -> dict:
        """Place a SELL limit order to close a position.

        Uses the existing ClobTrader infrastructure — no MATIC needed.
        """
        from src.execution.clob_trader import ClobTrader, TradeOrder

        trader = ClobTrader()
        order = TradeOrder(
            token_id=pos.token_id,
            side="SELL",
            price=round(sell_price, 2),
            size=round(pos.size, 2),
            market_slug=pos.market_slug,
            market_question=pos.bracket,
            ev_per_share=sell_price - pos.avg_price,
            kelly_fraction=0.0,
            p_model=0.0,
        )
        result = trader.place_limit_order(
            order=order,
            tick_size=0.01,
            min_size=5.0,
            mid_price=sell_price,
            dry_run=False,
        )
        return {
            "success": result.success,
            "order_id": result.order_id,
            "status": result.status,
            "error": result.error,
            "side": "SELL",
            "price": sell_price,
            "size": pos.size,
            "market_slug": pos.market_slug,
            "station": pos.station,
            "bracket": pos.bracket,
        }

    def run_cycle(self, cycle_num: int, dry_run: bool = True) -> dict:
        """Run position management cycle.

        Called at the start of each sniper cycle.
        Returns summary dict.
        """
        print("\n[0] Managing existing positions...")
        positions = self.load_positions()
        if not positions:
            print("  No open positions.")
            return {"positions": 0, "sells": 0, "redeems": 0}

        positions = self.enrich_positions(positions)
        print(f"  Open positions: {len(positions)}")

        sells = []
        redeems = []
        holds = []

        for pos in positions:
            current_price = self.get_current_price(pos.token_id)
            should, reason = self.should_sell(pos, current_price)

            print(f"    {pos.station:5s} {pos.bracket[:40]:40s} | "
                  f"{pos.size:.1f} @ {pos.avg_price:.3f} | now={current_price:.3f} | {reason}")

            if pos.resolved:
                redeems.append(pos)
            elif should and not dry_run:
                # --- PRICE GUARD: skip sells at degenerate prices (resolved/illiquid) ---
                if current_price <= 0.001 or current_price >= 0.999:
                    print(f"    SKIP sell for {pos.station}: degenerate price {current_price:.3f} (resolved?)")
                    holds.append(pos)
                    continue
                result = self.sell_position(pos, current_price)
                if result["success"]:
                    sells.append(result)
                    print(f"    ✓ SOLD: {pos.size:.1f} @ {current_price:.2f} | order={result['order_id']}")
                    if self.audit:
                        self.audit.log_order(cycle_num, pos.bracket, "SELL", current_price, pos.size, False,
                                             status=result["status"], market_slug=pos.market_slug,
                                             station=pos.station, order_id=result.get("order_id", ""))
                else:
                    print(f"    ✗ SELL FAILED: {result.get('error', 'unknown')}")
            elif should and dry_run:
                sells.append({"dry_run": True, "station": pos.station, "bracket": pos.bracket,
                             "price": current_price, "size": pos.size})
                print(f"    DRY-RUN SELL: {pos.size:.1f} @ {current_price:.2f}")
            else:
                holds.append(pos)

        if self.audit:
            self.audit.log_info(cycle_num,
                f"Position management: {len(positions)} positions, "
                f"{len(sells)} sells, {len(redeems)} pending redemption, {len(holds)} holds")

        return {
            "positions": len(positions),
            "sells": len(sells),
            "redeems": len(redeems),
            "holds": len(holds),
        }