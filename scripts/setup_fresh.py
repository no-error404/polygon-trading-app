"""
Fresh setup for Polymarket + Rabby wallet.

This script does everything in the correct order:
1. Reads your private key + wallet address from credentials.yaml
2. Connects to the CLOB API with signature_type=1 (POLY_PROXY)
3. Derives API credentials
4. Saves them back to credentials.yaml
5. Checks balance

PREREQUISITES:
- You created a Polymarket account by connecting Rabby wallet
- You deposited USDC.e through the Polymarket website
- The website shows your balance (e.g. $50)
- You put your private key + wallet address in config/credentials.yaml

USAGE:
  1. Edit config/credentials.yaml:
     private_key: "0xYOUR_KEY"
     funder_address: "0xYOUR_ADDRESS"
     (leave api_key, api_secret, api_passphrase empty)

  2. Run: python3 scripts/setup_fresh.py

  3. When prompted, paste the PROXY WALLET ADDRESS that Polymarket
     shows on the website (Settings → your wallet address).
     This is NOT your Rabby address — it's the Polymarket proxy.
"""

import sys
import os
import yaml
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

CREDENTIALS_PATH = PROJECT_ROOT / "config" / "credentials.yaml"
CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def load_credentials():
    if not CREDENTIALS_PATH.exists():
        print(f"ERROR: {CREDENTIALS_PATH} not found.")
        print("Copy config/credentials.yaml.template to config/credentials.yaml")
        sys.exit(1)
    with open(CREDENTIALS_PATH) as f:
        creds = yaml.safe_load(f)
    if not creds.get("private_key") or "YOUR_" in str(creds.get("private_key", "")):
        print("ERROR: private_key not set in credentials.yaml")
        print("Open the file and paste your private key (from Rabby: Account → Export Private Key)")
        sys.exit(1)
    if not creds.get("funder_address") or "YOUR_" in str(creds.get("funder_address", "")):
        print("ERROR: funder_address not set in credentials.yaml")
        print("Open the file and paste your wallet address (the 0x... from Rabby)")
        sys.exit(1)
    return creds


def save_credentials(creds):
    with open(CREDENTIALS_PATH, "w") as f:
        yaml.safe_dump(creds, f, default_flow_style=False)
    print(f"Credentials saved to {CREDENTIALS_PATH}")


def main():
    print("=" * 60)
    print("FRESH SETUP — Polymarket + Rabby Wallet")
    print("=" * 60)

    creds = load_credentials()
    private_key = creds["private_key"]
    eoa_address = creds["funder_address"]

    print(f"\nEOA (your Rabby address): {eoa_address}")
    print(f"Private key: {'set (' + str(len(private_key)) + ' chars)'}")

    # Ask for the proxy wallet address shown on Polymarket website
    print()
    print("On the Polymarket website (logged in with Rabby):")
    print("  Go to Settings and find your wallet address")
    print("  This is NOT your Rabby address (%s)" % eoa_address)
    print("  It is a DIFFERENT address — the Polymarket proxy wallet")
    print("  It should look like: 0x157F0453492B326DC3E995DC3C898768184Ee6d9")
    print()
    proxy_address = input("Paste your Polymarket proxy wallet address: ").strip()

    if not proxy_address or not proxy_address.startswith("0x") or len(proxy_address) != 42:
        print("ERROR: Invalid address. Must be 42 chars starting with 0x")
        sys.exit(1)

    print(f"\nProxy wallet: {proxy_address}")

    # Connect to CLOB with proxy wallet + sig_type=1 (POLY_PROXY)
    print("\n--- Connecting to CLOB API ---")
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

    # First, derive API creds (no existing creds needed for this)
    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        signature_type=1,
        funder=proxy_address,
    )

    print("Deriving API credentials...")
    try:
        api_creds = client.create_or_derive_api_creds()
        print(f"API key: {api_creds.api_key}")
        client.set_api_creds(api_creds)
    except Exception as e:
        print(f"ERROR deriving creds: {e}")
        sys.exit(1)

    # Save credentials
    creds["api_key"] = api_creds.api_key
    creds["api_secret"] = api_creds.api_secret
    creds["api_passphrase"] = api_creds.api_passphrase
    save_credentials(creds)

    # Check balance
    print("\n--- Checking balance ---")
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=1)
    bal = client.get_balance_allowance(params)
    balance = bal.get("balance", "0")
    print(f"CLOB balance: {balance}")

    if balance != "0":
        print(f"\nSUCCESS! Balance: {float(balance) / 1e6:.2f} USDC")
    else:
        print("\nBalance shows 0 in CLOB API.")
        print("But if the website shows your balance, try placing a trade")
        print("through the website first — this registers the proxy with the CLOB.")
        print("Then re-run this script.")

    # Try a test order with v2 client
    print("\n--- Test order: BUY 5 @ 0.01 (won't fill, safe) ---")
    try:
        from py_clob_client_v2.client import ClobClient as ClobClientV2
        from py_clob_client_v2.clob_types import ApiCreds as ApiCredsV2, OrderArgs as OrderArgsV2, OrderType as OrderTypeV2
        from py_clob_client.clob_types import ApiCreds

        client_v2 = ClobClientV2(
            host=CLOB_HOST,
            key=private_key,
            chain_id=POLYGON_CHAIN_ID,
            creds=ApiCreds(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                api_passphrase=creds["api_passphrase"],
            ),
            signature_type=1,
            funder=proxy_address,
        )

        markets = client_v2.get_sampling_markets()
        m = markets["data"][0]
        token_id = m["tokens"][0]["token_id"]
        print(f"Market: {m['question']}")
        print(f"Token: {token_id[:30]}...")

        order_args = OrderArgsV2(token_id=token_id, price=0.01, size=5, side="BUY")
        signed_order = client_v2.create_order(order_args)
        print(f"Order maker: {signed_order.maker}")
        print(f"Order signer: {signed_order.signer}")
        print(f"Order sigType: {signed_order.signatureType}")

        resp = client_v2.post_order(signed_order, order_type=OrderTypeV2.GTC)
        print(f"Order response: {resp}")
    except Exception as e:
        print(f"Order error: {e}")
        if "maker address not allowed" in str(e):
            print()
            print("The proxy wallet isn't registered with the CLOB yet.")
            print("FIX: On the Polymarket website, place a small trade manually")
            print("(e.g. $1 on any market). This registers the proxy wallet")
            print("with the CLOB's matching engine. Then re-run this script.")

    print("\n" + "=" * 60)
    print("Setup complete. Credentials saved.")
    print("=" * 60)


if __name__ == "__main__":
    main()