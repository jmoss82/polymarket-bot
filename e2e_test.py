"""
End-to-end test: derive creds → check balance → find market → place $1 order.

Run on Railway (EU) to bypass US geo-blocking.
Each step prints PASS/FAIL so you know exactly where it breaks.

Usage:  python -u e2e_test.py
"""
import json
import sys
import traceback
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, OrderArgs, BalanceAllowanceParams, AssetType,
)
from config import POLY_PRIVATE_KEY, POLY_FUNDER, CHAIN_ID, CLOB_HOST

GAMMA_API = "https://gamma-api.polymarket.com"
BET_AMOUNT = 1.0  # dollars — tiny test order


# ── Pretty printing ──────────────────────────────────────────
def step(n, label):
    print(f"\n{'='*50}", flush=True)
    print(f"STEP {n}: {label}", flush=True)
    print(f"{'='*50}", flush=True)

def ok(msg=""):
    print(f"  >> PASS {msg}", flush=True)

def fail(msg=""):
    print(f"  >> FAIL {msg}", flush=True)


# ── Main test ────────────────────────────────────────────────
async def main():
    print("=" * 50, flush=True)
    print("POLYMARKET E2E TEST", flush=True)
    print(f"Goal: place a ${BET_AMOUNT:.0f} live order", flush=True)
    print("=" * 50, flush=True)

    funder = POLY_FUNDER

    # ── Step 1: Private key ──────────────────────────────────
    step(1, "Private key + wallet address")
    if not POLY_PRIVATE_KEY:
        fail("POLY_PRIVATE_KEY is not set")
        return

    print(f"  PK loaded: {POLY_PRIVATE_KEY[:10]}...", flush=True)

    try:
        from eth_account import Account
        derived_addr = Account.from_key(POLY_PRIVATE_KEY).address
        print(f"  Derived address: {derived_addr}", flush=True)

        if funder:
            if derived_addr.lower() == funder.lower():
                print(f"  POLY_FUNDER matches: {funder}", flush=True)
            else:
                fail(f"POLY_FUNDER mismatch!")
                print(f"    POLY_FUNDER  = {funder}", flush=True)
                print(f"    Derived addr = {derived_addr}", flush=True)
                print(f"  Fix: set POLY_FUNDER={derived_addr}", flush=True)
                return
        else:
            print(f"  POLY_FUNDER not set — using derived address", flush=True)
            funder = derived_addr

        ok()
    except ImportError:
        print("  WARNING: eth_account not installed, cannot verify address", flush=True)
        if not funder:
            fail("POLY_FUNDER is not set and eth_account unavailable")
            return
        ok("(address not verified)")

    # ── Step 2: Derive L2 API credentials ────────────────────
    step(2, "Derive L2 API credentials (IP-bound)")
    try:
        l1_client = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=POLY_PRIVATE_KEY,
            signature_type=0,
        )
        creds = l1_client.derive_api_key()

        if isinstance(creds, dict):
            api_key = creds.get("apiKey") or creds.get("api_key")
            api_secret = creds.get("secret") or creds.get("api_secret")
            api_passphrase = creds.get("passphrase") or creds.get("api_passphrase")
        else:
            api_key = creds.api_key
            api_secret = creds.api_secret
            api_passphrase = creds.api_passphrase

        print(f"  API Key:    {api_key[:20]}...", flush=True)
        print(f"  Secret:     {api_secret[:20]}...", flush=True)
        print(f"  Passphrase: {api_passphrase[:20]}...", flush=True)
        ok()
    except Exception as e:
        fail(str(e))
        traceback.print_exc()
        return

    # ── Step 3: Auth check ───────────────────────────────────
    step(3, "Authenticate with derived creds")
    try:
        client = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=POLY_PRIVATE_KEY,
            creds=ApiCreds(api_key, api_secret, api_passphrase),
            funder=funder,
            signature_type=0,
        )
        keys_resp = client.get_api_keys()
        print(f"  get_api_keys(): {keys_resp}", flush=True)
        ok()
    except Exception as e:
        fail(str(e))
        traceback.print_exc()
        return

    # ── Step 4: USDC balance ─────────────────────────────────
    step(4, "USDC balance & allowance")
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params)
        print(f"  Raw response: {bal}", flush=True)

        # Try to extract a human-readable balance
        if isinstance(bal, dict):
            raw_bal = bal.get("balance", "0")
            try:
                usdc = float(raw_bal) / 1e6   # USDC = 6 decimals
                print(f"  Parsed USDC: ${usdc:.6f}", flush=True)
                if usdc < BET_AMOUNT:
                    fail(f"Balance ${usdc:.2f} < ${BET_AMOUNT:.2f} needed")
                    print(f"  Deposit USDC to your Polymarket wallet first", flush=True)
                    return
            except (ValueError, TypeError):
                print(f"  Could not parse balance — continuing anyway", flush=True)

        ok()
    except Exception as e:
        fail(str(e))
        traceback.print_exc()
        print("  Continuing anyway — balance format may differ", flush=True)

    # ── Step 5: Find an active market ────────────────────────
    step(5, "Find active BTC 15-min market")
    market = None
    found_slug = None

    async with aiohttp.ClientSession() as session:
        now = datetime.now(timezone.utc)
        mins = (now.minute // 15) * 15
        base = now.replace(minute=mins, second=0, microsecond=0)

        for offset in [0, 15, -15]:
            ts = int((base + timedelta(minutes=offset)).timestamp())
            slug = f"btc-updown-15m-{ts}"
            url = f"{GAMMA_API}/markets"
            try:
                async with session.get(
                    url,
                    params={"slug": slug},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data and len(data) > 0:
                        m = data[0]
                        active = m.get("active")
                        accepting = m.get("acceptingOrders")
                        closed = m.get("closed")
                        print(f"  {slug}:", flush=True)
                        print(f"    active={active}  acceptingOrders={accepting}  closed={closed}", flush=True)
                        if active and accepting and not closed:
                            market = m
                            found_slug = slug
                            break
                    else:
                        print(f"  {slug}: not found", flush=True)
            except Exception as e:
                print(f"  {slug}: error — {e}", flush=True)

    if not market:
        fail("No active market found that is accepting orders")
        print("  Markets may be between intervals. Try again in a few minutes.", flush=True)
        return

    question = market.get("question", "?")
    clob_ids = json.loads(market.get("clobTokenIds", "[]"))
    prices = json.loads(market.get("outcomePrices", "[]"))
    min_size = market.get("orderMinSize")

    if len(prices) < 2 or len(clob_ids) < 2:
        fail(f"Market data incomplete: prices={prices}, clob_ids={clob_ids}")
        return

    print(f"  Market:   {question}", flush=True)
    print(f"  Up price: {prices[0]}   Down price: {prices[1]}", flush=True)
    print(f"  Min size: {min_size}", flush=True)
    ok(f"Using {found_slug}")

    # ── Step 6: Place the order ──────────────────────────────
    step(6, f"Place ${BET_AMOUNT:.0f} BUY order")

    # Pick the cheaper side (easier to fill, less capital at risk)
    price_up = float(prices[0])
    price_down = float(prices[1])

    if price_down <= price_up:
        side_label = "Down"
        token_id = clob_ids[1]
        raw_price = price_down
    else:
        side_label = "Up"
        token_id = clob_ids[0]
        raw_price = price_up

    # Round price to 2 decimals (Polymarket tick size = 0.01)
    price = round(raw_price, 2)
    if price < 0.01:
        price = 0.01
    if price > 0.99:
        price = 0.99

    size = round(BET_AMOUNT / price, 2)

    # Respect minimum order size if known
    if min_size:
        try:
            ms = float(min_size)
            if size < ms:
                print(f"  Bumping size from {size} to min {ms}", flush=True)
                size = ms
        except (ValueError, TypeError):
            pass

    print(f"  Side:     BUY {side_label}", flush=True)
    print(f"  Token:    {token_id[:40]}...", flush=True)
    print(f"  Price:    {price}", flush=True)
    print(f"  Size:     {size} shares", flush=True)
    print(f"  Cost:     ~${price * size:.2f}", flush=True)

    try:
        result = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
        )
        print(f"  Response: {result}", flush=True)

        if result and isinstance(result, dict):
            order_id = result.get("orderID")
            success = result.get("success")
            error_msg = result.get("errorMsg") or result.get("error")

            if order_id:
                ok(f"Order placed! ID: {order_id}")
            elif success is False:
                fail(f"Rejected: {error_msg or result}")
            else:
                print(f"  Unexpected response shape — check above", flush=True)
                ok("(got response)")
        elif result:
            print(f"  Non-dict result: {type(result)} = {result}", flush=True)
            ok("(got response)")
        else:
            fail("Empty response from create_and_post_order")

    except Exception as e:
        fail(str(e))
        traceback.print_exc()
        return

    # ── Done ─────────────────────────────────────────────────
    print(f"\n{'='*50}", flush=True)
    print("E2E TEST COMPLETE", flush=True)
    print(f"{'='*50}", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
    except Exception as e:
        print(f"\n[FATAL] {e}", flush=True)
        traceback.print_exc()
    sys.exit(0)
