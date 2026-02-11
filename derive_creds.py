"""Derive User L2 API credentials from private key."""
import sys
from py_clob_client.client import ClobClient
from config import POLY_PRIVATE_KEY, CLOB_HOST, CHAIN_ID

print("Deriving user API credentials...", flush=True)
print(f"PK: {POLY_PRIVATE_KEY[:8]}...", flush=True)

try:
    client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=POLY_PRIVATE_KEY,
    )
    print("Client created (L1 only)", flush=True)

    creds = client.derive_api_key()
    print("\n=== NEW USER API CREDENTIALS ===", flush=True)
    print(f"API Key:      {creds.api_key}", flush=True)
    print(f"API Secret:   {creds.api_secret}", flush=True)
    print(f"Passphrase:   {creds.api_passphrase}", flush=True)
    print("================================", flush=True)
    print("\nPut these in your .env as POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE", flush=True)

except Exception as e:
    print(f"Error: {e}", flush=True)
    import traceback
    traceback.print_exc()
