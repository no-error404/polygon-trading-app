#!/usr/bin/env python3
"""
fund_gas.py — Transfer MATIC from EOA to deposit wallet for gas.

The deposit wallet (0x157F...) needs MATIC to execute on-chain
redemption transactions. The EOA (0x0623...) has ~81 MATIC.
"""
import sys
import yaml
from pathlib import Path
from web3 import Web3

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT) if False else None  # suppress lint
import os; os.chdir(PROJECT_ROOT)

RPC = "https://polygon-bor-rpc.publicnode.com"
AMOUNT_MATIC = 3.0

def main():
    with open("config/credentials.yaml") as f:
        creds = yaml.safe_load(f)
    with open("config/settings.yaml") as f:
        settings = yaml.safe_load(f)

    eoa = creds["funder_address"]
    deposit = settings["trading"]["deposit_wallet"]
    private_key = creds["private_key"]

    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))
    assert w3.is_connected(), "RPC not connected"

    eoa_bal = w3.from_wei(w3.eth.get_balance(w3.to_checksum_address(eoa)), "ether")
    dep_bal = w3.from_wei(w3.eth.get_balance(w3.to_checksum_address(deposit)), "ether")
    print(f"EOA MATIC: {eoa_bal:.6f}")
    print(f"Deposit MATIC: {dep_bal:.6f}")

    if dep_bal >= 0.5:
        print(f"Deposit wallet already has {dep_bal:.4f} MATIC — no transfer needed.")
        return

    gas_price = w3.eth.gas_price
    print(f"Current gas: {w3.from_wei(gas_price, 'gwei'):.1f} gwei")

    tx = {
        "to": w3.to_checksum_address(deposit),
        "value": w3.to_wei(AMOUNT_MATIC, "ether"),
        "nonce": w3.eth.get_transaction_count(w3.to_checksum_address(eoa)),
        "gas": 21000,
        "maxFeePerGas": w3.to_wei(600, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(60, "gwei"),
        "chainId": 137,
    }
    print(f"Sending {AMOUNT_MATIC} MATIC to deposit wallet...")
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print(f"Status: {receipt['status']} (1=success)")
    dep_after = w3.from_wei(w3.eth.get_balance(w3.to_checksum_address(deposit)), "ether")
    print(f"Deposit MATIC now: {dep_after:.6f}")

if __name__ == "__main__":
    main()