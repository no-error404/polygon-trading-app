"""
Approve USDC.e spending for Polymarket contracts.

This script sends an on-chain ERC-20 approve transaction so that
Polymarket's exchange contract can move your USDC.e on your behalf
when you place orders.

This is a one-time operation. Once approved, you don't need to run
this again unless you revoke the approval.

USAGE:
  python3 scripts/approve_usdc.py

REQUIRES: web3 (pip install web3)
"""

import sys
import json
import yaml
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os_chdir = PROJECT_ROOT
import os
os.chdir(PROJECT_ROOT)

CREDENTIALS_PATH = PROJECT_ROOT / "config" / "credentials.yaml"
RPC_URL = "https://polygon-bor-rpc.publicnode.com"

# The CORRECT USDC.e contract that Polymarket uses
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket contract addresses
# NOTE: py-clob-client's get_exchange_address returns 0x4bFb... but the CLOB API
# actually checks allowances for 0xE111... and 0xe222... These are the proxy/router
# contracts that the CLOB uses to move funds. We approve ALL of them to be safe.
POLYMARKET_CONTRACTS = [
    ("CLOB Exchange Proxy", "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("CLOB Neg Risk Adapter", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
    ("CLOB Neg Risk Adapter 2", "0xe2222d279d744050d28e00520010520000310F59"),
    # Also approve the ones py-clob-client reports (for direct order signing)
    ("Exchange (py-clob)", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("Conditional (py-clob)", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
]

# ERC-20 approve ABI
APPROVE_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]


def main():
    from web3 import Web3

    print("=" * 60)
    print("USDC.e Approval for Polymarket")
    print("=" * 60)

    # Load credentials
    with open(CREDENTIALS_PATH) as f:
        creds = yaml.safe_load(f)
    private_key = creds["private_key"]
    wallet_address = creds["funder_address"]

    # Connect to Polygon
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("ERROR: Cannot connect to Polygon RPC")
        sys.exit(1)
    print(f"Connected to Polygon (block {w3.eth.block_number})")

    # USDC.e contract
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_E_CONTRACT),
        abi=APPROVE_ABI,
    )

    # Check current balance
    balance = usdc.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call()
    print(f"USDC.e balance: {balance / 1e6:.6f}")

    if balance == 0:
        print("ERROR: No USDC.e balance to approve. Fund the wallet first.")
        sys.exit(1)

    # Check current allowance for each Polymarket contract
    print()
    for name, addr in POLYMARKET_CONTRACTS:
        spender = Web3.to_checksum_address(addr)
        current = usdc.functions.allowance(
            Web3.to_checksum_address(wallet_address), spender
        ).call()
        print(f"Current allowance for {name} ({addr[:10]}...): {current / 1e6:.2f} USDC.e")

    # Approve max uint256 for all contracts
    MAX_UINT256 = 2**256 - 1
    print()
    print("Approving MAX_UINT256 for all Polymarket contracts...")

    nonce = w3.eth.get_transaction_count(Web3.to_checksum_address(wallet_address))
    gas_price = w3.eth.gas_price

    for name, addr in POLYMARKET_CONTRACTS:
        spender = Web3.to_checksum_address(addr)
        # Skip if already approved
        current = usdc.functions.allowance(
            Web3.to_checksum_address(wallet_address), spender
        ).call()
        if current > 0:
            print(f"  {name} ({addr[:10]}...) already approved, skipping")
            continue

        print(f"  Approving {name} ({addr[:10]}...)...", end=" ", flush=True)

        # Build transaction
        tx = usdc.functions.approve(spender, MAX_UINT256).build_transaction({
            "from": Web3.to_checksum_address(wallet_address),
            "nonce": nonce,
            "gas": 100000,
            "gasPrice": gas_price,
            "chainId": 137,
        })

        # Sign and send
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"tx: {tx_hash.hex()}")

        # Wait for receipt
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        status = "SUCCESS" if receipt["status"] == 1 else "FAILED"
        print(f"    Status: {status}, gas used: {receipt['gasUsed']}")

        nonce += 1

    # Verify allowances are now set
    print()
    print("--- Verifying allowances ---")
    for name, addr in POLYMARKET_CONTRACTS:
        spender = Web3.to_checksum_address(addr)
        current = usdc.functions.allowance(
            Web3.to_checksum_address(wallet_address), spender
        ).call()
        approved = "YES" if current > 0 else "NO"
        print(f"  {name}: approved={approved} ({current / 1e6:.2f} USDC.e)")

    print()
    print("=" * 60)
    print("DONE — USDC.e approved for Polymarket")
    print("Now re-run: python3 scripts/derive_api_creds.py")
    print("=" * 60)


if __name__ == "__main__":
    main()