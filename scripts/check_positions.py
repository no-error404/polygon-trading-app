#!/usr/bin/env python3
"""
check_positions.py — Diagnostic: what does the bot actually own?

Uses condition IDs from CLOB trade data (not slug queries).
Uses working Polygon RPC endpoints.
Checks ERC1155 token balances for the deposit wallet.
Determines neg-risk vs regular CTF for each market.

NEVER prints credentials.
"""

import sys
import os
import json
import yaml
import requests
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# --- Config (silent load, never printed) ---
with open("config/credentials.yaml") as f:
    creds = yaml.safe_load(f)
with open("config/settings.yaml") as f:
    settings = yaml.safe_load(f)

PRIVATE_KEY = creds.get("private_key", "")
DEPOSIT_WALLET = settings.get("trading", {}).get(
    "deposit_wallet", "0x157F0453492B326DC3E995DC3C898768184Ee6d9"
)
FUNDER = creds.get("funder_address", DEPOSIT_WALLET)

# Contract addresses
CTF_CONTRACT = "0x4D97DCd97eC9458406432Ffd2703E9c1e4cB7c79"       # ConditionalTokens
NEG_RISK_ADAPTER = "0xC5d563A36eA459D34212a593CfaCF8c5b3cE4e7e"   # NegRiskAdapter
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa8414E"

# Working Polygon RPC endpoints (polygon-rpc.com requires auth now)
RPC_ENDPOINTS = [
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-bor-rpc.publicnode.com",
]


def get_working_rpc():
    """Find a working Polygon RPC."""
    from web3 import Web3
    for url in RPC_ENDPOINTS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                return w3, url
        except Exception:
            continue
    return None, None


def main():
    print("=" * 70)
    print("POSITION DIAGNOSTIC")
    print("=" * 70)
    print(f"Deposit wallet: {DEPOSIT_WALLET}")
    print(f"Funder/EOA:     {FUNDER}")
    print()

    # --- 1. CLOB trades ---
    print("[1] Querying CLOB trades...")
    from py_clob_client_v2.client import ClobClient as ClobClientV2
    from py_clob_client_v2.clob_types import ApiCreds as ApiCredsV2

    creds_obj = ApiCredsV2(
        api_key=creds["api_key"],
        api_secret=creds["api_secret"],
        api_passphrase=creds["api_passphrase"],
    )
    client = ClobClientV2(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=137,
        creds=creds_obj,
        signature_type=3,
        funder=DEPOSIT_WALLET,
    )

    # Get trades — paginated returns a dict with 'trades' key
    resp = client.get_trades_paginated()
    all_trades = resp.get("trades", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
    print(f"  Total trades: {len(all_trades)}")

    # Group by condition ID (the 'market' field) — aggregate net position per token
    positions = {}  # condition_id -> {asset_id, outcome, total_size, trades: []}
    for t in all_trades:
        cid = t.get("market", "")
        aid = t.get("asset_id", "")
        side = t.get("side", "")
        size = float(t.get("size", 0))
        outcome = t.get("outcome", "?")

        key = f"{cid}:{aid}"
        if key not in positions:
            positions[key] = {
                "condition_id": cid,
                "asset_id": aid,
                "outcome": outcome,
                "net_size": 0.0,
                "trades": [],
            }
        if side == "BUY":
            positions[key]["net_size"] += size
        else:
            positions[key]["net_size"] -= size
        positions[key]["trades"].append({
            "side": side, "size": size, "price": float(t.get("price", 0)),
            "status": t.get("status"), "outcome": outcome,
        })

    print(f"  Unique positions (by condition+token): {len(positions)}")
    for key, pos in positions.items():
        print(f"    {pos['condition_id'][:16]}... token={pos['asset_id'][:16]}... "
              f"outcome={pos['outcome']} net={pos['net_size']:.4f} "
              f"({len(pos['trades'])} trades)")
    print()

    # --- 2. USDC balance ---
    print("[2] USDC balance...")
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=3,
        )
        bal = client.get_balance_allowance(params)
        if bal:
            balance_usdc = float(bal.get("balance", "0")) / 1e6
            print(f"  USDC balance: ${balance_usdc:.6f}")
        else:
            print("  Balance: None returned")
    except Exception as e:
        print(f"  Balance ERROR: {e}")
    print()

    # --- 3. Web3 on-chain checks ---
    print("[3] Web3 on-chain checks...")
    w3, rpc_url = get_working_rpc()
    if not w3:
        print("  ERROR: No working Polygon RPC found!")
        return
    print(f"  Using RPC: {rpc_url}")
    print(f"  Block: {w3.eth.block_number}")

    # MATIC balance for both addresses
    for label, addr in [("funder/EOA", FUNDER), ("deposit_wallet", DEPOSIT_WALLET)]:
        try:
            matic_bal = w3.eth.get_balance(w3.to_checksum_address(addr))
            print(f"  {label} {addr}: MATIC={w3.from_wei(matic_bal, 'ether'):.6f}")
        except Exception as e:
            print(f"  {label} {addr}: ERROR: {e}")
    print()

    # --- 4. ERC1155 (CTF) token balances ---
    print("[4] ERC1155 (CTF) token balances...")
    erc1155_abi = [
        {
            "constant": True,
            "inputs": [
                {"name": "account", "type": "address"},
                {"name": "id", "type": "uint256"},
            ],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [
                {"name": "accounts", "type": "address[]"},
                {"name": "ids", "type": "uint256[]"},
            ],
            "name": "balanceOfBatch",
            "outputs": [{"name": "", "type": "uint256[]"}],
            "type": "function",
        },
    ]
    ctf = w3.eth.contract(
        address=w3.to_checksum_address(CTF_CONTRACT),
        abi=erc1155_abi,
    )

    # Check CTF balances for each traded token at the deposit wallet
    for key, pos in positions.items():
        if pos["net_size"] <= 0:
            continue  # no position
        token_id_int = int(pos["asset_id"])
        try:
            bal = ctf.functions.balanceOf(
                w3.to_checksum_address(DEPOSIT_WALLET),
                token_id_int,
            ).call()
            print(f"  {pos['condition_id'][:16]}... outcome={pos['outcome']:>6s} "
                  f"token={pos['asset_id'][:16]}... "
                  f"CTF balance={bal} (expected ~{pos['net_size']:.2f})")
        except Exception as e:
            print(f"  {pos['condition_id'][:16]}... ERROR: {e}")

    # Also check neg-risk adapter balances if applicable
    # The NegRiskAdapter may hold tokens for neg-risk markets
    print()
    print("  Checking NegRiskAdapter balances too...")
    neg_risk_ctf = w3.eth.contract(
        address=w3.to_checksum_address(NEG_RISK_ADAPTER),
        abi=erc1155_abi,
    )
    for key, pos in positions.items():
        if pos["net_size"] <= 0:
            continue
        token_id_int = int(pos["asset_id"])
        try:
            bal = neg_risk_ctf.functions.balanceOf(
                w3.to_checksum_address(DEPOSIT_WALLET),
                token_id_int,
            ).call()
            if bal > 0:
                print(f"  NegRiskAdapter {pos['condition_id'][:16]}... "
                      f"outcome={pos['outcome']:>6s} balance={bal}")
            else:
                print(f"  NegRiskAdapter {pos['condition_id'][:16]}... "
                      f"outcome={pos['outcome']:>6s} balance=0")
        except Exception as e:
            print(f"  NegRiskAdapter {pos['condition_id'][:16]}... ERROR: {e}")
    print()

    # --- 5. Gamma API market resolution status ---
    print("[5] Gamma API market status (by condition ID)...")
    condition_ids = set(pos["condition_id"] for pos in positions.values())
    market_info = []
    for cid in condition_ids:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"conditionID": cid},
                timeout=15,
            )
            data = r.json()
            if not data:
                # Try alternate param format
                r = requests.get(
                    f"https://gamma-api.polymarket.com/markets?condition_id={cid}",
                    timeout=15,
                )
                data = r.json()
            if not data:
                print(f"  {cid[:20]}...: NOT FOUND")
                continue
            m = data[0]
            info = {
                "condition_id": cid,
                "slug": m.get("slug", ""),
                "question": m.get("question", m.get("title", "?")),
                "closed": m.get("closed"),
                "resolved": m.get("resolved"),
                "negRisk": m.get("negRisk", False),
                "outcomes": m.get("outcomes", []),
                "outcomePrices": m.get("outcomePrices", m.get("outcome_prices", [])),
                "clobTokenIds": m.get("clobTokenIds", m.get("clobTokenIds", [])),
                "negRiskMarketId": m.get("negRiskMarketId", ""),
                "negRiskRequestId": m.get("negRiskRequestId", ""),
                "isArchived": m.get("isArchived"),
                "resolutionSource": m.get("resolutionSource", ""),
            }
            market_info.append(info)
            print(f"  {cid[:20]}...")
            print(f"    question: {info['question']}")
            print(f"    slug: {info['slug']}")
            print(f"    closed={info['closed']}  resolved={info['resolved']}  negRisk={info['negRisk']}  archived={info['isArchived']}")
            print(f"    outcomes: {info['outcomes']}")
            print(f"    outcomePrices: {info['outcomePrices']}")
            print(f"    clobTokenIds: {info['clobTokenIds']}")
            if info.get("negRiskMarketId"):
                print(f"    negRiskMarketId: {info['negRiskMarketId']}")
            if info.get("negRiskRequestId"):
                print(f"    negRiskRequestId: {info['negRiskRequestId']}")
            print()
        except Exception as e:
            print(f"  {cid[:20]}... ERROR: {e}")
            print()

    # --- 6. Summary ---
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total trades: {len(all_trades)}")
    print(f"Unique positions: {len(positions)}")
    print(f"Markets found on Gamma: {len(market_info)}")
    print()
    for info in market_info:
        print(f"  {info['slug']}")
        print(f"    closed={info['closed']} resolved={info['resolved']} negRisk={info['negRisk']}")
        if info["negRisk"]:
            print(f"    negRiskMarketId={info['negRiskMarketId']}")
        print()

    print("DESIGN DECISIONS FOR REDEEMER:")
    neg_risk_markets = [i for i in market_info if i.get("negRisk")]
    regular_markets = [i for i in market_info if not i.get("negRisk")]
    print(f"  Neg-risk markets: {len(neg_risk_markets)} → use NegRiskAdapter.redeem()")
    print(f"  Regular CTF markets: {len(regular_markets)} → use ConditionalTokens.redeemPositions()")


if __name__ == "__main__":
    main()