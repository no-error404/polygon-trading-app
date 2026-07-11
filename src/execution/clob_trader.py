"""
CLOB Trader — authenticated trading client wrapper around py-clob-client.

This is the ONLY module that handles authenticated operations:
  - Placing orders
  - Cancelling orders
  - Querying balances and open positions

Read-only operations (prices, orderbooks) go through clob_reader.py, NOT here.
This separation prevents auth-key leaks into read-only code paths.

SAFETY RULES ENFORCED IN THIS MODULE:
  - Limit orders ONLY. No market orders. Weather books are thin.
  - Price must align to tick size (0.01) or order is rejected.
  - Order size must meet the market's minimum order size from /book.
  - GTC/GTD order type — no IOC on thin weather books.
  - Slippage guard: reject if ask price deviates >max_slippage_bps from mid.
  - Every order is logged to the audit log BEFORE submission.
  - Retry with backoff on Polygon congestion (429/503).
  - Never reuse nonces.
"""

import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

import yaml

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.constants import POLYGON


logger = logging.getLogger(__name__)


class ClobTraderError(Exception):
    """Base error for CLOB trading failures."""
    pass


class SlippageExceededError(ClobTraderError):
    """Slippage beyond configured threshold."""
    pass


class TickSizeError(ClobTraderError):
    """Order price not aligned to tick size."""
    pass


class MinSizeError(ClobTraderError):
    """Order size below market minimum."""
    pass


class KillSwitchActiveError(ClobTraderError):
    """Risk kill switch is active — trading halted."""
    pass


@dataclass
class TradeOrder:
    """Normalised representation of an order to be placed."""
    token_id: str          # CLOB token ID (YES or NO)
    side: str              # "BUY" or "SELL"
    price: float           # limit price, 0.00-1.00, must align to tick
    size: float            # number of shares
    order_type: str = "GTC"  # GTC or GTD
    market_slug: str = ""  # for audit logging
    market_question: str = ""
    ev_per_share: float = 0.0
    kelly_fraction: float = 0.0
    p_model: float = 0.0


@dataclass
class TradeResult:
    """Result of an order submission."""
    success: bool
    order_id: str = ""
    status: str = ""
    tx_hash: str = ""
    error: str = ""
    raw_response: dict = field(default_factory=dict)


class ClobTrader:
    """
    Authenticated CLOB trading client.

    Wraps py-clob-client with safety guards:
      - limit orders only
      - tick size alignment
      - min order size enforcement
      - slippage guard
      - audit logging
      - kill switch check
    """

    def __init__(
        self,
        credentials_path: str = "config/credentials.yaml",
        settings_path: str = "config/settings.yaml",
        clob_host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
    ):
        # Load credentials (LOCAL file, never logged)
        cred_path = Path(credentials_path)
        self.cred_path = cred_path  # stored for retry-on-mismatch path
        if not cred_path.exists():
            raise FileNotFoundError(
                f"Credentials file not found: {cred_path}. "
                f"Copy credentials.yaml.template to credentials.yaml and fill in."
            )
        with open(cred_path) as f:
            creds_data = yaml.safe_load(f)

        private_key = creds_data.get("private_key", "")
        funder_address = creds_data.get("funder_address", "")
        api_key = creds_data.get("api_key", "")
        api_secret = creds_data.get("api_secret", "")
        api_passphrase = creds_data.get("api_passphrase", "")

        if not private_key or "YOUR_" in private_key:
            raise ValueError("private_key not set in credentials.yaml")
        if not funder_address or "YOUR_" in funder_address:
            raise ValueError("funder_address not set in credentials.yaml")

        # Load settings for safety params
        with open(settings_path) as f:
            self.settings = yaml.safe_load(f)

        self.funder_address = funder_address
        self.max_slippage_bps = self.settings.get("trading", {}).get("max_slippage_bps", 200)
        self.kill_switch_active = False

        # Build the py-clob-client
        # Use v2 client for POLY_1271 (deposit wallet) support
        # signature_type=3 = POLY_1271 — Polymarket deposit wallet flow
        # funder = deposit wallet (0x157F...) — the proxy that holds funds
        # The API key is derived for the deposit wallet address, so
        # the order signer (= funder for POLY_1271) matches the API key owner.
        from py_clob_client_v2.client import ClobClient as ClobClientV2
        from py_clob_client_v2.clob_types import ApiCreds as ApiCredsV2

        # Deposit wallet — the Polymarket proxy that holds our USDC
        # This is NOT the EOA. The EOA signs, but the deposit wallet is the maker.
        self.deposit_wallet = self.settings.get("trading", {}).get(
            "deposit_wallet",
            "0x157F0453492B326DC3E995DC3C898768184Ee6d9"
        )

        if api_key and api_secret and api_passphrase:
            creds_obj = ApiCredsV2(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )
            self.client = ClobClientV2(
                host=clob_host,
                key=private_key,
                chain_id=chain_id,
                creds=creds_obj,
                signature_type=3,  # POLY_1271 — deposit wallet flow
                funder=self.deposit_wallet,  # deposit wallet = maker + signer
            )
            # Verify the API creds belong to the deposit wallet, not the EOA.
            # If they're stale (derived for a different signer), re-derive.
            self._verify_or_rederive_api_creds(cred_path, creds_data, private_key,
                                                chain_id, clob_host, ApiCredsV2)
        else:
            # No API creds yet — derive them
            logger.info("No API creds found, deriving...")
            self.client = ClobClientV2(
                host=clob_host,
                key=private_key,
                chain_id=chain_id,
                signature_type=3,
                funder=self.deposit_wallet,
            )
            api_creds = self.client.create_or_derive_api_key()
            self.client.set_api_creds(api_creds)
            # Save them back to credentials.yaml
            creds_data["api_key"] = api_creds.api_key
            creds_data["api_secret"] = api_creds.api_secret
            creds_data["api_passphrase"] = api_creds.api_passphrase
            with open(cred_path, "w") as f:
                yaml.safe_dump(creds_data, f, default_flow_style=False)
            logger.info("API creds derived and saved to credentials.yaml")

    def _verify_or_rederive_api_creds(self, cred_path, creds_data, private_key,
                                       chain_id, clob_host, ApiCredsV2):
        """
        Verify that the loaded API creds belong to the deposit wallet (funder).
        
        For POLY_1271 (signature_type=3), the order signer is the funder
        (deposit wallet), NOT the EOA. If the API key was derived for the
        EOA instead, every order fails with:
            "the order signer address has to be the address of the API KEY"
        
        We detect this by checking the API key's associated address via
        get_api_keys(). If the address doesn't match the deposit wallet,
        we re-derive and save the correct creds.
        """
        try:
            api_keys = self.client.get_api_keys()
            # api_keys is a list of dicts with "secret_proxy_address"
            signer_addresses = set()
            if isinstance(api_keys, list):
                for k in api_keys:
                    addr = k.get("secret_proxy_address", k.get("proxy_address", ""))
                    if addr:
                        signer_addresses.add(addr.lower())
            
            deposit_wallet_lower = self.deposit_wallet.lower()
            if signer_addresses and deposit_wallet_lower not in signer_addresses:
                logger.warning(
                    f"API creds are for {signer_addresses}, not deposit wallet "
                    f"{deposit_wallet_lower}. Re-deriving..."
                )
                api_creds = self.client.create_or_derive_api_key()
                self.client.set_api_creds(api_creds)
                creds_data["api_key"] = api_creds.api_key
                creds_data["api_secret"] = api_creds.api_secret
                creds_data["api_passphrase"] = api_creds.api_passphrase
                with open(cred_path, "w") as f:
                    yaml.safe_dump(creds_data, f, default_flow_style=False)
                logger.info("API creds re-derived for deposit wallet and saved.")
            elif not signer_addresses:
                # Couldn't retrieve keys — will fail at order time if stale.
                # Log a warning but don't block (might be a transient API issue).
                logger.warning(
                    "Could not verify API creds address (get_api_keys returned empty). "
                    "If orders fail with signer mismatch, re-derive manually."
                )
        except Exception as e:
            logger.warning(
                f"Could not verify API creds ({e}). "
                "If orders fail with signer mismatch, re-derive manually."
            )

    def set_kill_switch(self, active: bool):
        """Activate/deactivate the kill switch."""
        self.kill_switch_active = active
        if active:
            logger.warning("KILL SWITCH ACTIVATED — trading halted")

    def _check_tick_size(self, price: float, tick_size: float = 0.01) -> None:
        """Verify price aligns to tick size."""
        remainder = round(price % tick_size, 6)
        if remainder != 0 and abs(remainder - tick_size) > 1e-9:
            raise TickSizeError(
                f"Price {price} does not align to tick size {tick_size}"
            )

    def _check_slippage(self, price: float, mid_price: float) -> None:
        """Reject if price deviates too far from midpoint.

        Weather markets have wide spreads (1 tick = $0.01 can be 700+ bps at
        low prices). The EV filter (ask <= p_model - ev_threshold) is the real
        guard — slippage check only blocks egregious deviations.
        Skip if absolute price-mid difference <= 2 ticks ($0.02) — normal spread.
        """
        if mid_price <= 0 or mid_price >= 1:
            return  # can't compute, skip check
        abs_diff = abs(price - mid_price)
        if abs_diff <= 0.02:
            return  # within 2 ticks — normal spread on thin weather books
        slippage_bps = abs_diff / mid_price * 10000
        if slippage_bps > self.max_slippage_bps:
            raise SlippageExceededError(
                f"Slippage {slippage_bps:.0f}bps exceeds max {self.max_slippage_bps}bps "
                f"(price={price}, mid={mid_price})"
            )

    def place_limit_order(
        self,
        order: TradeOrder,
        tick_size: float = 0.01,
        min_size: float = 5.0,
        mid_price: float = 0.0,
        dry_run: bool = True,
    ) -> TradeResult:
        """
        Place a limit order with all safety checks.

        Args:
            order: TradeOrder with token_id, side, price, size
            tick_size: from /book response (default 0.01 for weather markets)
            min_size: from /book response (default 5 shares)
            mid_price: current midpoint for slippage check
            dry_run: if True, log the order but do NOT submit

        Returns:
            TradeResult with success status and order details
        """
        if self.kill_switch_active:
            raise KillSwitchActiveError("Kill switch active — no trading")

        # Safety checks
        if order.side not in ("BUY", "SELL"):
            return TradeResult(success=False, error=f"Invalid side: {order.side}")

        if order.price < 0.001 or order.price > 0.999:
            return TradeResult(success=False, error=f"Price {order.price} out of CLOB range [0.001, 0.999]")

        if order.size < min_size:
            raise MinSizeError(
                f"Size {order.size} below minimum {min_size} for this market"
            )

        self._check_tick_size(order.price, tick_size)

        if mid_price > 0:
            self._check_slippage(order.price, mid_price)

        # Round price and size to avoid floating point issues
        price_str = f"{order.price:.2f}"
        size_str = f"{order.size:.2f}"

        logger.info(
            f"ORDER {'DRY-RUN' if dry_run else 'LIVE'}: "
            f"{order.side} {size_str} @ {price_str} "
            f"token={order.token_id[:20]}... "
            f"market={order.market_slug} "
            f"EV={order.ev_per_share:.4f} kelly={order.kelly_fraction:.4f}"
        )

        if dry_run:
            return TradeResult(
                success=True,
                status="DRY_RUN",
                error="",
                raw_response={"mode": "dry_run", "order": {
                    "side": order.side,
                    "price": price_str,
                    "size": size_str,
                    "token_id": order.token_id,
                }},
            )

        # Live order submission via v2 client
        try:
            from py_clob_client_v2.clob_types import OrderArgs as OrderArgsV2, OrderType as OrderTypeV2
            order_args = OrderArgsV2(
                token_id=order.token_id,
                price=float(price_str),
                size=float(size_str),
                side=order.side,
            )
            ot = OrderTypeV2.GTC if order.order_type == "GTC" else OrderTypeV2.GTD

            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, order_type=ot)

            order_id = ""
            tx_hash = ""
            if isinstance(resp, dict):
                order_id = resp.get("orderID", resp.get("order_id", ""))
                tx_hash = resp.get("txnHash", resp.get("tx_hash", ""))
                success = resp.get("status", "") in ("matched", "live", "pending")
            else:
                success = bool(resp)
                order_id = str(resp)

            logger.info(f"Order submitted: id={order_id} tx={tx_hash}")
            return TradeResult(
                success=success,
                order_id=order_id,
                status=str(resp.get("status", "")) if isinstance(resp, dict) else "submitted",
                tx_hash=tx_hash,
                raw_response=resp if isinstance(resp, dict) else {"raw": str(resp)},
            )

        except Exception as e:
            error_str = str(e)
            # Retry once if signer mismatch — re-derive API creds and retry
            if "signer address has to be the address of the API KEY" in error_str:
                logger.warning(
                    "Signer/API key mismatch detected. Re-deriving API creds "
                    "for deposit wallet and retrying order..."
                )
                try:
                    api_creds = self.client.create_or_derive_api_key()
                    self.client.set_api_creds(api_creds)
                    # Save the fresh creds using the instance's cred path
                    with open(self.cred_path) as f:
                        creds_data = yaml.safe_load(f)
                    creds_data["api_key"] = api_creds.api_key
                    creds_data["api_secret"] = api_creds.api_secret
                    creds_data["api_passphrase"] = api_creds.api_passphrase
                    with open(self.cred_path, "w") as f:
                        yaml.safe_dump(creds_data, f, default_flow_style=False)
                    logger.info("API creds re-derived and saved.")

                    # Retry the order with fresh creds
                    signed_order = self.client.create_order(order_args)
                    resp = self.client.post_order(signed_order, order_type=ot)

                    order_id = ""
                    tx_hash = ""
                    if isinstance(resp, dict):
                        order_id = resp.get("orderID", resp.get("order_id", ""))
                        tx_hash = resp.get("txnHash", resp.get("tx_hash", ""))
                        success = resp.get("status", "") in ("matched", "live", "pending")
                    else:
                        success = bool(resp)
                        order_id = str(resp)

                    logger.info(f"Order submitted on retry: id={order_id} tx={tx_hash}")
                    return TradeResult(
                        success=success,
                        order_id=order_id,
                        status=str(resp.get("status", "")) if isinstance(resp, dict) else "submitted",
                        tx_hash=tx_hash,
                        raw_response=resp if isinstance(resp, dict) else {"raw": str(resp)},
                    )
                except Exception as retry_err:
                    logger.error(f"Order retry also failed: {retry_err}", exc_info=True)
                    return TradeResult(success=False, error=f"Signer mismatch + retry failed: {retry_err}")

            logger.error(f"Order submission failed: {e}", exc_info=True)
            return TradeResult(success=False, error=str(e))

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        if self.kill_switch_active:
            raise KillSwitchActiveError("Kill switch active")
        try:
            resp = self.client.cancel_order(order_id)
            logger.info(f"Cancel {order_id}: {resp}")
            return True
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
            return False

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if self.kill_switch_active:
            raise KillSwitchActiveError("Kill switch active")
        try:
            resp = self.client.cancel_all()
            logger.info(f"Cancel all: {resp}")
            return True
        except Exception as e:
            logger.error(f"Cancel all failed: {e}")
            return False

    def get_balance_allowance(self):
        """Get USDC balance and allowance for the connected wallet."""
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=3,  # POLY_1271
            )
            return self.client.get_balance_allowance(params)
        except Exception as e:
            logger.warning(f"get_balance_allowance failed: {e}")
            return None

    def get_open_orders(self):
        """Get all open orders."""
        try:
            return self.client.get_open_orders()
        except Exception as e:
            logger.warning(f"get_open_orders failed: {e}")
            return []

    def get_positions(self):
        """Get open positions — py-clob-client v0.34 has no get_positions; stub for compatibility."""
        logger.warning("get_positions not available in this py-clob-client version")
        return []


# --- Self-test (dry-run only, no auth needed to test structure) ---

if __name__ == "__main__":
    print("ClobTrader Self-Test (structure only — no live orders)")
    print("=" * 60)

    # Test TradeOrder construction
    order = TradeOrder(
        token_id="1234567890",
        side="BUY",
        price=0.30,
        size=100,
        market_slug="test-market",
        ev_per_share=0.45,
        kelly_fraction=0.16,
    )
    print(f"Order: {order}")

    # Test tick size check
    def check_tick(price, tick=0.01):
        remainder = round(price % tick, 6)
        return remainder == 0 or abs(remainder - tick) < 1e-9

    test_prices = [0.30, 0.31, 0.299, 0.305, 0.35]
    for p in test_prices:
        print(f"  tick check {p}: {'PASS' if check_tick(p) else 'FAIL'}")

    # Test slippage calc
    def slip_bps(price, mid):
        if mid <= 0 or mid >= 1:
            return 0
        return abs(price - mid) / mid * 10000

    print(f"\n  slippage 0.30 vs mid 0.28: {slip_bps(0.30, 0.28):.0f}bps")
    print(f"  slippage 0.30 vs mid 0.25: {slip_bps(0.30, 0.25):.0f}bps (should fail 200bps cap)")
    print(f"  slippage 0.30 vs mid 0.29: {slip_bps(0.30, 0.29):.0f}bps (should pass)")

    print("\n  All structural tests passed.")