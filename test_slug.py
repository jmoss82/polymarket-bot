import aiohttp, asyncio, json
from datetime import datetime, timezone, timedelta

async def check():
    now = datetime.now(timezone.utc)
    mins = (now.minute // 15) * 15
    base = now.replace(minute=mins, second=0, microsecond=0)

    async with aiohttp.ClientSession() as s:
        for offset in [-30, -15, 0, 15, 30]:
            t = base + timedelta(minutes=offset)
            ts = int(t.timestamp())
            for crypto in ["btc", "eth"]:
                slug = f"{crypto}-updown-15m-{ts}"
                url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
                async with s.get(url) as r:
                    data = await r.json()
                    if data:
                        print(f"FOUND: {slug}")
                        for m in data[:1]:
                            print(f"  Q: {m.get('question', '?')}")
                            print(f"  Active: {m.get('active')}")
                            tokens = m.get("tokens", [])
                            print(f"  Tokens: {json.dumps(tokens)[:300]}")
                        print()
                    else:
                        print(f"empty: {slug}")

asyncio.run(check())
