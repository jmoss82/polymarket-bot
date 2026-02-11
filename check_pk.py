import sys
import inspect
print("start", flush=True)

from py_clob_client.client import ClobClient
from config import POLY_PRIVATE_KEY

pk = POLY_PRIVATE_KEY
print(f"PK: {pk[:10]}...", flush=True)

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key=pk,
)
print("Client created", flush=True)

# List available methods related to api_key
methods = [m for m in dir(client) if 'api' in m.lower() or 'derive' in m.lower() or 'create' in m.lower()]
print(f"Available methods: {methods}", flush=True)

# Try create_or_derive first
for method_name in ['create_or_derive_api_key', 'create_api_key', 'derive_api_key']:
    if hasattr(client, method_name):
        print(f"\nTrying {method_name}...", flush=True)
        try:
            method = getattr(client, method_name)
            result = method()
            print(f"  SUCCESS: {result}", flush=True)
            if hasattr(result, 'api_key'):
                print(f"  API Key:    {result.api_key}", flush=True)
                print(f"  Secret:     {result.api_secret}", flush=True)
                print(f"  Passphrase: {result.api_passphrase}", flush=True)
            break
        except Exception as e:
            print(f"  Failed: {e}", flush=True)
