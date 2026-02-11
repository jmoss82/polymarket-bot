import aiohttp, asyncio, json
from datetime import datetime, timezone, timedelta

async def check():
    now = datetime.now(timezone.utc)
    mins = (now.minute // 15) * 15
    base = now.replace(minute=mins, second=0, microsecond=0)
    ts = int(base.timestamp())
    slug = f"btc-updown-15m-{ts}"
    
    async with aiohttp.ClientSession() as s:
        # Get full market detail
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        async with s.get(url) as r:
            data = await r.json()
            if data:
                print(json.dumps(data[0], indent=2)[:3000])

asyncio.run(check())
