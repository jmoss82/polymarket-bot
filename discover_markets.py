"""
Discover active 15-minute Up/Down markets on Polymarket.
Uses slug-based lookup against the Gamma API (the general search/filter doesn't index these).

Slug pattern: {asset}-updown-15m-{unix_timestamp}
Where timestamp = start of the 15-min interval in UTC.
"""
import asyncio
import aiohttp
import json
from datetime import datetime, timezone, timedelta
from config import CLOB_HOST

GAMMA_API = "https://gamma-api.polymarket.com"

ASSETS = ["btc", "eth", "sol", "xrp"]


def get_interval_timestamps(offsets=(-15, 0, 15, 30)):
    """Generate unix timestamps for 15-min intervals around now."""
    now = datetime.now(timezone.utc)
    mins = (now.minute // 15) * 15
    base = now.replace(minute=mins, second=0, microsecond=0)
    return [(base + timedelta(minutes=o), int((base + timedelta(minutes=o)).timestamp())) for o in offsets]


async def fetch_market_by_slug(session, slug):
    """Fetch a single market by its slug. Returns dict or None."""
    url = f"{GAMMA_API}/markets"
    async with session.get(url, params={"slug": slug}) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        return data[0] if data else None


async def get_active_updown_markets(assets=None):
    """
    Find active 15-min Up/Down markets by constructing slugs for
    current and nearby intervals.
    """
    if assets is None:
        assets = ASSETS

    intervals = get_interval_timestamps()
    markets = []

    async with aiohttp.ClientSession() as session:
        tasks = []
        for asset in assets:
            for dt, ts in intervals:
                slug = f"{asset}-updown-15m-{ts}"
                tasks.append((asset, dt, ts, slug, fetch_market_by_slug(session, slug)))

        for asset, dt, ts, slug, coro in tasks:
            market = await coro
            if market and market.get("active") and not market.get("closed"):
                # Parse token IDs
                clob_ids = json.loads(market.get("clobTokenIds", "[]"))
                outcome_prices = json.loads(market.get("outcomePrices", "[]"))

                markets.append({
                    "id": market["id"],
                    "slug": slug,
                    "question": market.get("question"),
                    "asset": asset.upper(),
                    "interval_start": dt.isoformat(),
                    "end_date": market.get("endDate"),
                    "condition_id": market.get("conditionId"),
                    "question_id": market.get("questionID"),
                    "token_id_up": clob_ids[0] if len(clob_ids) > 0 else None,
                    "token_id_down": clob_ids[1] if len(clob_ids) > 1 else None,
                    "price_up": float(outcome_prices[0]) if len(outcome_prices) > 0 else None,
                    "price_down": float(outcome_prices[1]) if len(outcome_prices) > 1 else None,
                    "volume": market.get("volumeNum"),
                    "liquidity": market.get("liquidityNum"),
                    "accepting_orders": market.get("acceptingOrders"),
                    "order_min_size": market.get("orderMinSize"),
                    "neg_risk": market.get("negRisk"),
                    "resolution_source": market.get("resolutionSource"),
                })

    return markets


async def main():
    now_et = datetime.now(timezone(timedelta(hours=-5)))
    print("=" * 60)
    print(f"15-Min Up/Down Market Discovery  |  {now_et.strftime('%I:%M %p ET')}")
    print("=" * 60)

    markets = await get_active_updown_markets()

    if not markets:
        print("\nNo active 15-min markets found right now.")
        print("They may not be running at this hour.")
        return

    print(f"\nFound {len(markets)} active markets:\n")
    for m in markets:
        direction = "UP" if m["price_up"] and m["price_up"] > 0.5 else "DN"
        print(f"  {direction} {m['question']}")
        print(f"     Up: {m['price_up']:.2f}  |  Down: {m['price_down']:.2f}  |  Vol: ${m['volume']:,.0f}  |  Liq: ${m['liquidity']:,.0f}")
        print(f"     Token UP:   {m['token_id_up'][:20]}..." if m['token_id_up'] else "")
        print(f"     Token DOWN: {m['token_id_down'][:20]}..." if m['token_id_down'] else "")
        print(f"     Resolution: {m['resolution_source']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
