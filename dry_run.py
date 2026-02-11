"""
Dry run: find a live market, create a signed order, but DON'T post it.
Validates the full flow minus actual execution.
"""
import json
import traceback
import aiohttp
import asyncio
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from config import *

GAMMA_API = "https://gamma-api.polymarket.com"


async def find_live_market():
    """Find a currently active BTC 15-min market."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    mins = (now.minute // 15) * 15
    base = now.replace(minute=mins, second=0, microsecond=0)

    async with aiohttp.ClientSession() as session:
        for offset in [0, 15]:
            ts = int((base + timedelta(minutes=offset)).timestamp())
            slug = f"btc-updown-15m-{ts}"
            url = f"{GAMMA_API}/markets"
            async with session.get(url, params={"slug": slug}) as resp:
                data = await resp.json()
                if data and data[0].get("active") and not data[0].get("closed"):
                    return data[0], slug
    return None, None


async def main():
    print("=== DRY RUN TEST ===\n", flush=True)

    # Step 1: Find a live market
    print("1. Finding live BTC market...", flush=True)
    market, slug = await find_live_market()
    if not market:
        print("   No active market found. Try during trading hours.", flush=True)
        return

    question = market.get("question", "?")
    clob_ids = json.loads(market.get("clobTokenIds", "[]"))
    prices = json.loads(market.get("outcomePrices", "[]"))
    print(f"   Found: {question}", flush=True)
    print(f"   Up: {prices[0]}  Down: {prices[1]}", flush=True)
    print(f"   Token Up:   {clob_ids[0][:30]}...", flush=True)
    print(f"   Token Down: {clob_ids[1][:30]}...", flush=True)
    print(f"   Accepting orders: {market.get('acceptingOrders')}", flush=True)
    print(f"   Min size: {market.get('orderMinSize')}", flush=True)

    # Step 2: Create CLOB client
    print("\n2. Creating authenticated client...", flush=True)
    client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=POLY_PRIVATE_KEY,
        creds=ApiCreds(POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE),
        funder=POLY_FUNDER,
        signature_type=0,
    )
    print("   Client OK", flush=True)

    # Step 3: Create (but don't post) an order
    print("\n3. Creating signed order (NOT posting)...", flush=True)
    token_id = clob_ids[1]  # Down token
    price = float(prices[1])
    size = round(5.0 / price, 2)  # $5 worth of shares

    print(f"   Side: BUY Down", flush=True)
    print(f"   Price: {price}", flush=True)
    print(f"   Size: {size} shares", flush=True)
    print(f"   Cost: ~${price * size:.2f}", flush=True)

    try:
        # create_order builds and signs but does NOT submit
        signed_order = client.create_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
        )
        print(f"   Signed order created!", flush=True)
        print(f"   Order: {json.dumps(signed_order, default=str)[:500]}", flush=True)
    except Exception as e:
        print(f"   Order creation failed: {e}", flush=True)
        traceback.print_exc()
        return

    # Step 4: Test posting for real (the actual live test)
    print("\n4. POST test (actual order submission)...", flush=True)
    print("   SKIPPED — this is a dry run.", flush=True)
    print("   To go live, use: client.post_order(signed_order)", flush=True)

    print("\n=== DRY RUN COMPLETE — Everything looks good! ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
