"""Test actual order submission to see if API geo-blocks."""
import json
import traceback
import asyncio
import aiohttp
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from config import *

GAMMA_API = "https://gamma-api.polymarket.com"

async def find_market():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    mins = (now.minute // 15) * 15
    base = now.replace(minute=mins, second=0, microsecond=0)
    async with aiohttp.ClientSession() as s:
        for offset in [0, 15]:
            ts = int((base + timedelta(minutes=offset)).timestamp())
            slug = f"btc-updown-15m-{ts}"
            async with s.get(f"{GAMMA_API}/markets", params={"slug": slug}) as r:
                data = await r.json()
                if data and data[0].get("active"):
                    return data[0]
    return None

async def main():
    print("Finding market...", flush=True)
    market = await find_market()
    if not market:
        print("No active market", flush=True)
        return

    clob_ids = json.loads(market.get("clobTokenIds", "[]"))
    prices = json.loads(market.get("outcomePrices", "[]"))
    print(f"Market: {market['question']}", flush=True)
    print(f"Down price: {prices[1]}", flush=True)

    client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=POLY_PRIVATE_KEY,
        creds=ApiCreds(POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE),
        funder=POLY_FUNDER,
        signature_type=0,
    )

    token_id = clob_ids[1]
    price = float(prices[1])
    size = round(5.0 / price, 2)

    print(f"\nPosting REAL order: BUY {size} Down @ {price}...", flush=True)
    try:
        result = client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
        )
        print(f"Result: {result}", flush=True)
    except Exception as e:
        print(f"Error: {e}", flush=True)
        traceback.print_exc()

asyncio.run(main())
