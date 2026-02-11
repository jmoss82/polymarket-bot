"""Test Polymarket API authentication with User L2 creds."""
import traceback
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from config import *

print("Connecting to Polymarket CLOB...", flush=True)

try:
    client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=POLY_PRIVATE_KEY,
        creds=ApiCreds(POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE),
        funder=POLY_FUNDER,
        signature_type=0,
    )
    print("Client created OK", flush=True)

    print("\n1. Server time:", flush=True)
    t = client.get_server_time()
    print(f"   {t}", flush=True)

    print("\n2. API keys for this wallet:", flush=True)
    keys = client.get_api_keys()
    print(f"   {keys}", flush=True)

    print("\n3. Open orders:", flush=True)
    orders = client.get_orders()
    print(f"   {orders}", flush=True)

    print("\nAll checks passed! Auth is working.", flush=True)

except Exception as e:
    print(f"\nERROR: {e}", flush=True)
    traceback.print_exc()
