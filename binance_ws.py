"""
Binance WebSocket — real-time BTC/ETH trade stream.
Fires a callback on every trade with (symbol, price, timestamp_ms).
Robust reconnect + callback error isolation.
"""
import asyncio
import json
import time
import traceback
import websockets
from config import BINANCE_WS_URL, BINANCE_STREAMS


class BinanceFeed:
    def __init__(self, symbols=None, on_trade=None):
        self.symbols = symbols or list(BINANCE_STREAMS.keys())
        self.on_trade = on_trade
        self.last_prices = {}
        self._running = False
        self._trade_count = 0
        self._connect_count = 0

    async def start(self):
        streams = "/".join(BINANCE_STREAMS[s] for s in self.symbols)
        url = f"{BINANCE_WS_URL}/{streams}"
        self._running = True
        print(f"[BinanceFeed] Connecting to {url}")

        while self._running:
            try:
                self._connect_count += 1
                async with websockets.connect(
                    url,
                    ping_interval=30,
                    ping_timeout=20,
                    close_timeout=10,
                    max_size=2**20,
                ) as ws:
                    if self._connect_count > 1:
                        print(f"[BinanceFeed] Reconnected (attempt #{self._connect_count})")
                    else:
                        print("[BinanceFeed] Connected")

                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            symbol = data["s"].replace("USDT", "")
                            price = float(data["p"])
                            exchange_ts = data["T"]
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
                            # Isolate callback/parse errors — never kill the connection
                            print(f"[BinanceFeed] Callback error: {e}")

            except asyncio.CancelledError:
                print("[BinanceFeed] Cancelled")
                break
            except Exception as e:
                if self._running:
                    print(f"[BinanceFeed] Disconnected: {e}. Reconnecting in 3s...")
                    await asyncio.sleep(3)

    def stop(self):
        self._running = False


async def _demo_callback(symbol, price, exchange_ts, local_ts):
    latency = local_ts - exchange_ts
    print(f"  {symbol} ${price:,.2f}  (latency: {latency}ms)")


if __name__ == "__main__":
    feed = BinanceFeed(on_trade=_demo_callback)
    try:
        asyncio.run(feed.start())
    except KeyboardInterrupt:
        feed.stop()
        print("\nStopped.")
