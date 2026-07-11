"""
Derive Polymarket API credentials from your wallet private key.

PREREQUISITES:
  1. Copy config/credentials.yaml.template to config/credentials.yaml
  2. Open config/credentials.yaml in a text editor
  3. Fill in private_key (from MetaMask: Account Details → Show Private Key)
  4. Fill in funder_address (your wallet address, the 0x... at the top of MetaMask)
  5. Leave api_key, api_secret, api_passphrase empty — this script fills them

USAGE:
  python3 scripts/derive_api_creds.py

This script:
  - Reads your private key from config/credentials.yaml (LOCAL file, never sent anywhere except Polymarket)
  - Connects to the CLOB API
  - Derives your L2 API credentials (api_key, api_secret, api_passphrase)
  - Writes them back into config/credentials.yaml
  - Tests the connection by fetching your USDC balance and open orders

NEVER commit config/credentials.yaml to git — it is gitignored.
NEVER paste your private key into any chat or terminal that logs.
"""

import sys
import os
import yaml
from pathlib import Path

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

CREDENTIALS_PATH = PROJECT_ROOT / "config" / "credentials.yaml"
CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9469Aa54124"  # USDC.e on Polygon (Polymarket uses this)


def load_credentials():
    if not CREDENTIALS_PATH.exists():
        print(f"ERROR: {CREDENTIALS_PATH} not found.")
        print("Copy config/credentials.yaml.template to config/credentials.yaml")
        print("and fill in your private_key and funder_address.")
        sys.exit(1)
    with open(CREDENTIALS_PATH) as f:
        creds = yaml.safe_load(f)
    if not creds.get("private_key") or "YOUR_" in str(creds.get("private_key")):
        print("ERROR: private_key not set in credentials.yaml")
        print("Open the file and paste your private key (from MetaMask Account Details).")
        sys.exit(1)
    if not creds.get("funder_address") or "YOUR_" in str(creds.get("funder_address")):
        print("ERROR: funder_address not set in credentials.yaml")
        print("Open the file and paste your wallet address (the 0x... from MetaMask).")
        sys.exit(1)
    return creds


def save_credentials(creds):
    with open(CREDENTIALS_PATH, "w") as f:
        yaml.safe_dump(creds, f, default_flow_style=False)
    print(f"Credentials saved to {CREDENTIALS_PATH}")


def derive_api_creds(private_key):
    """Use py-clob-client to derive L2 API credentials."""
    from py_clob_client.client import ClobClient

    print("Connecting to CLOB API to derive credentials...")
    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
    )
    # Derive or create API creds
    # create_or_derive_api_creds handles both first-time creation and existing
    api_creds = client.create_or_derive_api_creds()
    print("API credentials derived successfully.")
    return client, api_creds


def test_connection(client, funder_address):
    """Test the CLOB connection by fetching balance and open orders."""
    print("\n--- Testing CLOB connection ---")
    print(f"Wallet address: {funder_address}")

    # Get USDC balance + allowance via the CLOB client
    # get_balance_allowance requires BalanceAllowanceParams with asset_type=COLLATERAL
    # signature_type=0 means EOA (externally owned account) — our MetaMask wallet
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=0,  # 0 = EOA
        )
        bal = client.get_balance_allowance(params)
        print(f"USDC balance + allowance: {bal}")
    except Exception as e:
        print(f"get_balance_allowance() warning: {e}")

    # Fetch open orders as a connection test
    try:
        orders = client.get_orders()
        print(f"Open orders: {len(orders) if orders else 0}")
    except Exception as e:
        print(f"get_orders() warning: {e}")

    print("Connection test complete.")


def main():
    print("=" * 60)
    print("Polymarket API Credential Derivation")
    print("=" * 60)

    creds = load_credentials()
    private_key = creds["private_key"]
    funder_address = creds["funder_address"]

    # Check if API creds already exist
    has_api_creds = (
        creds.get("api_key")
        and creds.get("api_secret")
        and creds.get("api_passphrase")
        and "YOUR_" not in str(creds.get("api_key", ""))
    )

    if has_api_creds:
        print("API credentials already present in credentials.yaml.")
        print("Skipping derivation. To re-derive, clear the api_* fields.")
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        client = ClobClient(
            host=CLOB_HOST,
            key=private_key,
            chain_id=POLYGON_CHAIN_ID,
            creds=ApiCreds(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                api_passphrase=creds["api_passphrase"],
            ),
        )
    else:
        client, api_creds = derive_api_creds(private_key)
        # Save derived creds
        creds["api_key"] = api_creds.api_key
        creds["api_secret"] = api_creds.api_secret
        creds["api_passphrase"] = api_creds.api_passphrase
        save_credentials(creds)
        # Re-create client with creds set
        client.set_api_creds(api_creds)

    test_connection(client, funder_address)

    print("\n" + "=" * 60)
    print("DONE — credentials are in config/credentials.yaml")
    print("This file is gitignored. Never share it.")
    print("=" * 60)


if __name__ == "__main__":
    main()