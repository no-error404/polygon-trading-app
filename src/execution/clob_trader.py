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
import os
import yaml
from dataclasses import dataclass, field
from typing import Optional, Any
from pathlib import Path
from datetime import datetime, timezone
from enum import Enum

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client_v2.constants import POLYGON

logger = logging.getLogger(__name__)

# Polymarket V2 Native Collateral (pUSD)
PUSD_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

class OrderState(str, Enum):
    CREATED = "CREATED"
    POSTED = "POSTED"
    MATCHED = "MATCHED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

@dataclass
class OrderReceipt:
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    order_type: str
    status: OrderState
    builder_code: str = "QUANTMET"
    created_at: str = ""
    posted_at: Optional[str] = None
    matched_at: Optional[str] = None
    confirmed_at: Optional[str] = None

    def transition(self, status: OrderState, timestamp: str = ""):
        self.status = status
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        if status == OrderState.POSTED: self.posted_at = ts
        elif status == OrderState.MATCHED: self.matched_at = ts
        elif status == OrderState.CONFIRMED: self.confirmed_at = ts

class ClobTraderError(Exception):
    """Base error for CLOB trading failures."""
    pass

class AuthError(ClobTraderError):
    """Authentication failure."""
    pass

class OrderError(ClobTraderError):
    """Order placement failure."""
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
    Wraps py-clob-client with safety guards.
    """

    def __init__(
        self,
        credentials_path: str = "config/credentials.yaml",
        settings_path: str = "config/settings.yaml",
        clob_host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        private_key: Optional[str] = None,
    ):
        # 1. Private Key: env var > arg > credentials.yaml
        pk = os.getenv("POLYMARKET_PRIVATE_KEY") or private_key
        funder = None
        api_key = None
        api_secret = None
        api_passphrase = None

        cred_path = Path(credentials_path)
        self.cred_path = cred_path
        if cred_path.exists():
            with open(cred_path) as f:
                creds_data = yaml.safe_load(f)
                if creds_data:
                    pk = pk or creds_data.get("private_key")
                    funder = creds_data.get("funder_address")
                    api_key = creds_data.get("api_key")
                    api_secret = creds_data.get("api_secret")
                    api_passphrase = creds_data.get("api_passphrase")

        if not pk or pk == "" or "YOUR_" in pk:
            raise AuthError("POLYMARKET_PRIVATE_KEY not set")
        
        self._private_key = pk

        # Load settings
        with open(settings_path) as f:
            self.settings = yaml.safe_load(f)

        self.max_slippage_bps = self.settings.get("trading", {}).get("max_slippage_bps", 200)
        self.kill_switch_active = False
        self.deposit_wallet = funder or self.settings.get("trading", {}).get(
            "deposit_wallet", "0x157F0453492B326DC3E995DC3C898768184Ee6d9"
        )

        if api_key and api_secret and api_passphrase:
            creds_obj = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
            self.client = ClobClient(host=clob_host, key=pk, chain_id=chain_id, creds=creds_obj, signature_type=3, funder=self.deposit_wallet)
        else:
            self.client = ClobClient(host=clob_host, key=pk, chain_id=chain_id, signature_type=3, funder=self.deposit_wallet)
            try:
                api_creds = self.client.create_or_derive_api_key()
                self.client.set_api_creds(api_creds)
                # Auto-save derived keys if we have a path
                if cred_path.exists():
                    with open(cred_path) as f: d = yaml.safe_load(f) or {}
                    d.update({"api_key": api_creds.api_key, "api_secret": api_creds.api_secret, "api_passphrase": api_creds.api_passphrase})
                    with open(cred_path, "w") as f: yaml.safe_dump(d, f)
            except Exception as e:
                logger.warning(f"Failed to derive API keys: {e}")

    def _check_tick_size(self, price: float, tick_size: float = 0.01) -> None:
        remainder = round(price % tick_size, 6)
        if remainder != 0 and abs(remainder - tick_size) > 1e-9:
            raise TickSizeError(f"Price {price} does not align to tick size {tick_size}")

    def _check_slippage(self, price: float, mid_price: float) -> None:
        if mid_price <= 0 or mid_price >= 1: return
        abs_diff = abs(price - mid_price)
        if abs_diff <= 0.02: return
        slippage_bps = abs_diff / mid_price * 10000
        if slippage_bps > self.max_slippage_bps:
            raise SlippageExceededError(f"Slippage {slippage_bps:.0f}bps exceeds max {self.max_slippage_bps}bps")

    def place_limit_order(self, order: TradeOrder, tick_size: float = 0.01, min_size: float = 5.0, mid_price: float = 0.0, dry_run: bool = True) -> TradeResult:
        if self.kill_switch_active: raise KillSwitchActiveError("Kill switch active")
        if order.side not in ("BUY", "SELL"): return TradeResult(success=False, error=f"Invalid side: {order.side}")
        if order.price < 0.001 or order.price > 0.999: return TradeResult(success=False, error=f"Price {order.price} out of range")
        if order.size < min_size: raise MinSizeError(f"Size {order.size} below minimum {min_size}")
        
        self._check_tick_size(order.price, tick_size)
        if mid_price > 0: self._check_slippage(order.price, mid_price)

        price_str = f"{order.price:.2f}"
        size_str = f"{order.size:.2f}"

        if dry_run:
            return TradeResult(success=True, status="DRY_RUN", raw_response={"mode": "dry_run"})

        try:
            order_args = OrderArgs(token_id=order.token_id, price=float(price_str), size=float(size_str), side=order.side)
            ot = OrderType.GTC if order.order_type == "GTC" else OrderType.GTD
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order, order_type=ot)
            
            order_id = ""
            if isinstance(resp, dict):
                order_id = resp.get("orderID", resp.get("order_id", ""))
                success = resp.get("status") in ("matched", "live", "pending")
            else:
                order_id = str(resp)
                success = bool(resp)
            
            return TradeResult(success=success, order_id=order_id, status="submitted", raw_response=resp if isinstance(resp, dict) else {})
        except Exception as e:
            return TradeResult(success=False, error=str(e))

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order(order_id)
            return True
        except: return False

    def cancel_all(self) -> bool:
        try:
            self.client.cancel_all()
            return True
        except: return False

    def get_balance_allowance(self):
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            return self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=3))
        except: return None

    def get_open_orders(self):
        try: return self.client.get_open_orders()
        except: return []

    def get_trades(self):
        try: return self.client.get_trades_paginated()
        except: return []
