"""
Chainlink price feed via Polymarket's RTDS WebSocket.

Uses the crypto_prices_chainlink topic — the SAME Chainlink Data Streams
feed that Polymarket uses to resolve BTC 15-minute Up/Down markets.

Drop-in replacement for BinanceFeed: same callback signature
    on_trade(symbol, price, exchange_ts_ms, local_ts_ms)

Usage:
    feed = ChainlinkFeed(symbols=["BTC"], on_trade=my_callback)
    await feed.start()
"""
import asyncio
import json
import time
import websockets

RTDS_URL = "wss://ws-live-data.polymarket.com"

# Map our internal symbols to Chainlink's slash-separated format
CHAINLINK_SYMBOLS = {
    "BTC": "btc/usd",
    "ETH": "eth/usd",
    "SOL": "sol/usd",
    "XRP": "xrp/usd",
}


class ChainlinkFeed:
    def __init__(self, symbols=None, on_trade=None):
        self.symbols = symbols or ["BTC"]
        self.on_trade = on_trade
        self.last_prices = {}
        self._running = False
        self._trade_count = 0
        self._connect_count = 0

    async def start(self):
        self._running = True
        print(f"[ChainlinkFeed] Connecting to {RTDS_URL}")

        while self._running:
            try:
                self._connect_count += 1
                async with websockets.connect(
                    RTDS_URL,
                    ping_interval=5,
                    ping_timeout=10,
                    close_timeout=10,
                    max_size=2**20,
                ) as ws:
                    if self._connect_count > 1:
                        print(f"[ChainlinkFeed] Reconnected (attempt #{self._connect_count})")
                    else:
                        print("[ChainlinkFeed] Connected")

                    # Subscribe to Chainlink price feed
                    chainlink_syms = [CHAINLINK_SYMBOLS[s] for s in self.symbols
                                      if s in CHAINLINK_SYMBOLS]
                    filters = ",".join(chainlink_syms) if chainlink_syms else ""

                    sub_msg = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": json.dumps({"symbol": chainlink_syms[0]}) if len(chainlink_syms) == 1 else "",
                            }
                        ],
                    }
                    await ws.send(json.dumps(sub_msg))
                    print(f"[ChainlinkFeed] Subscribed: {chainlink_syms}", flush=True)

                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)

                            topic = data.get("topic", "")
                            if topic != "crypto_prices_chainlink":
                                continue

                            payload = data.get("payload", {})
                            raw_symbol = payload.get("symbol", "")
                            price = payload.get("value")
                            exchange_ts = payload.get("timestamp")

                            if price is None or exchange_ts is None:
                                continue

                            # Map Chainlink symbol back to our internal format
                            symbol = None
                            for k, v in CHAINLINK_SYMBOLS.items():
                                if v == raw_symbol:
                                    symbol = k
                                    break
                            if symbol is None:
                                continue

                            # Filter to requested symbols only
                            if symbol not in self.symbols:
                                continue

                            price = float(price)
                            exchange_ts = int(exchange_ts)
                            local_ts = int(time.time() * 1000)

                            self.last_prices[symbol] = {
                                "price": price,
                                "exchange_ts": exchange_ts,
                                "local_ts": local_ts,
                            }
                            self._trade_count += 1

                            if self.on_trade:
                                await self.on_trade(symbol, price, exchange_ts, local_ts)

                        except Exception as e:
                            print(f"[ChainlinkFeed] Parse error: {e}")

            except asyncio.CancelledError:
                print("[ChainlinkFeed] Cancelled")
                break
            except Exception as e:
                if self._running:
                    print(f"[ChainlinkFeed] Disconnected: {e}. Reconnecting in 3s...")
                    await asyncio.sleep(3)

    def stop(self):
        self._running = False


async def _demo_callback(symbol, price, exchange_ts, local_ts):
    latency = local_ts - exchange_ts
    print(f"  {symbol} ${price:,.2f}  (latency: {latency}ms)")


if __name__ == "__main__":
    feed = ChainlinkFeed(symbols=["BTC"], on_trade=_demo_callback)
    try:
        asyncio.run(feed.start())
    except KeyboardInterrupt:
        feed.stop()
        print("\nStopped.")
