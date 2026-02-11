"""
Polymarket WebSocket — order book and trade updates for 15-min Up/Down markets.
Uses the CLOB WebSocket API for real-time data.
"""
import asyncio
import json
import time
import websockets
from config import CLOB_HOST


# Polymarket CLOB WebSocket endpoint
WS_URL = CLOB_HOST.replace("https://", "wss://").replace("http://", "ws://") + "/ws"


class PolymarketFeed:
    def __init__(self, on_book_update=None, on_trade=None):
        self.on_book_update = on_book_update  # callback(market_id, book_data, local_ts)
        self.on_trade = on_trade              # callback(market_id, trade_data, local_ts)
        self.subscribed_markets = set()
        self._ws = None
        self._running = False

    async def start(self, market_ids=None):
        """Connect and subscribe to market updates."""
        self._running = True
        print(f"[PolymarketFeed] Connecting to {WS_URL}")

        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    self._ws = ws
                    print("[PolymarketFeed] Connected")

                    if market_ids:
                        await self.subscribe(market_ids)

                    async for msg in ws:
                        if not self._running:
                            break
                        local_ts = int(time.time() * 1000)
                        data = json.loads(msg)
                        await self._handle_message(data, local_ts)

            except (websockets.ConnectionClosed, Exception) as e:
                print(f"[PolymarketFeed] Disconnected: {e}. Reconnecting in 2s...")
                self._ws = None
                await asyncio.sleep(2)

    async def subscribe(self, market_ids):
        """Subscribe to order book + trade channels for given markets."""
        if not self._ws:
            print("[PolymarketFeed] Not connected, can't subscribe")
            return

        for market_id in market_ids:
            # Subscribe to book updates
            await self._ws.send(json.dumps({
                "type": "subscribe",
                "channel": "book",
                "market": market_id,
            }))
            # Subscribe to trade updates
            await self._ws.send(json.dumps({
                "type": "subscribe",
                "channel": "trades",
                "market": market_id,
            }))
            self.subscribed_markets.add(market_id)
            print(f"[PolymarketFeed] Subscribed to {market_id}")

    async def _handle_message(self, data, local_ts):
        channel = data.get("channel", "")
        market_id = data.get("market", "unknown")

        if channel == "book" and self.on_book_update:
            await self.on_book_update(market_id, data, local_ts)
        elif channel == "trades" and self.on_trade:
            await self.on_trade(market_id, data, local_ts)

    def stop(self):
        self._running = False


async def _demo_book(market_id, data, local_ts):
    print(f"  [BOOK] {market_id}: {json.dumps(data)[:200]}")


async def _demo_trade(market_id, data, local_ts):
    print(f"  [TRADE] {market_id}: {json.dumps(data)[:200]}")


if __name__ == "__main__":
    feed = PolymarketFeed(on_book_update=_demo_book, on_trade=_demo_trade)
    # You'd pass actual 15-min market token IDs here
    print("Run discover_markets.py first to get active market IDs")
    print("Then pass them to feed.start(market_ids=[...])")
