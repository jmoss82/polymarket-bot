"""
Phase 1: Data Collector
Runs both Binance and Polymarket WebSockets simultaneously.
Logs everything to files for analysis.

Usage:
    python collector.py
"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone

from binance_ws import BinanceFeed
from polymarket_ws import PolymarketFeed
from config import LOG_DIR, DATA_DIR


class DataCollector:
    def __init__(self, poly_market_ids=None):
        self.poly_market_ids = poly_market_ids or []
        self.binance_feed = BinanceFeed(on_trade=self._on_binance_trade)
        self.poly_feed = PolymarketFeed(
            on_book_update=self._on_poly_book,
            on_trade=self._on_poly_trade,
        )

        # Stats
        self.binance_count = 0
        self.poly_book_count = 0
        self.poly_trade_count = 0
        self.start_time = None

        # File handles
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._binance_file = open(f"{DATA_DIR}/binance_{date_str}.jsonl", "a")
        self._poly_book_file = open(f"{DATA_DIR}/poly_book_{date_str}.jsonl", "a")
        self._poly_trade_file = open(f"{DATA_DIR}/poly_trades_{date_str}.jsonl", "a")

    async def _on_binance_trade(self, symbol, price, exchange_ts, local_ts):
        self.binance_count += 1
        record = {
            "symbol": symbol,
            "price": price,
            "exchange_ts": exchange_ts,
            "local_ts": local_ts,
            "latency_ms": local_ts - exchange_ts,
        }
        self._binance_file.write(json.dumps(record) + "\n")

        # Print periodic stats
        if self.binance_count % 500 == 0:
            self._print_stats()

    async def _on_poly_book(self, market_id, data, local_ts):
        self.poly_book_count += 1
        record = {
            "market_id": market_id,
            "data": data,
            "local_ts": local_ts,
        }
        self._poly_book_file.write(json.dumps(record) + "\n")

    async def _on_poly_trade(self, market_id, data, local_ts):
        self.poly_trade_count += 1
        record = {
            "market_id": market_id,
            "data": data,
            "local_ts": local_ts,
        }
        self._poly_trade_file.write(json.dumps(record) + "\n")

    def _print_stats(self):
        elapsed = time.time() - self.start_time if self.start_time else 0
        binance_last = self.binance_feed.last_prices
        btc = binance_last.get("BTC", {}).get("price", "N/A")
        eth = binance_last.get("ETH", {}).get("price", "N/A")
        print(
            f"[{elapsed:.0f}s] "
            f"Binance: {self.binance_count} trades | "
            f"Poly book: {self.poly_book_count} | "
            f"Poly trades: {self.poly_trade_count} | "
            f"BTC: ${btc:,.2f} ETH: ${eth:,.2f}" if isinstance(btc, float) else
            f"[{elapsed:.0f}s] "
            f"Binance: {self.binance_count} trades | "
            f"Poly book: {self.poly_book_count} | "
            f"Poly trades: {self.poly_trade_count}"
        )

    async def run(self):
        self.start_time = time.time()
        print("=" * 60)
        print("  Polymarket 15-Min Data Collector")
        print("=" * 60)
        print(f"  Binance streams: BTC, ETH")
        print(f"  Polymarket markets: {len(self.poly_market_ids)} subscribed")
        print(f"  Data dir: {DATA_DIR}/")
        print("=" * 60)
        print()

        tasks = [
            asyncio.create_task(self.binance_feed.start()),
        ]

        if self.poly_market_ids:
            tasks.append(
                asyncio.create_task(self.poly_feed.start(self.poly_market_ids))
            )
        else:
            print("[!] No Polymarket market IDs provided.")
            print("    Run discover_markets.py first to find active 15-min markets.")
            print("    Starting Binance feed only...\n")

        # Periodic flush
        async def flush_loop():
            while True:
                await asyncio.sleep(10)
                self._binance_file.flush()
                self._poly_book_file.flush()
                self._poly_trade_file.flush()

        tasks.append(asyncio.create_task(flush_loop()))

        await asyncio.gather(*tasks)

    def cleanup(self):
        self._binance_file.close()
        self._poly_book_file.close()
        self._poly_trade_file.close()
        self.binance_feed.stop()
        self.poly_feed.stop()


if __name__ == "__main__":
    # TODO: Replace with actual market IDs from discover_markets.py
    market_ids = []  # e.g., ["0x1234...", "0x5678..."]

    collector = DataCollector(poly_market_ids=market_ids)
    try:
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        collector.cleanup()
        print("Data saved. ✓")
