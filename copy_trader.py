"""
Polymarket copy-trade bot — mirrors trades from target wallets.

Polls target wallets' recent trades via the public Data API,
then places matching orders via the CLOB.

Usage:  python -u copy_trader.py
Deploy: Railway (EU West) to bypass US geo-blocking.
"""
import sys
import signal
import asyncio
import traceback
import aiohttp

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from copy_config import (
    POLY_PRIVATE_KEY, POLY_FUNDER, CHAIN_ID, CLOB_HOST,
    COPY_TARGETS, COPY_SIZE_USD, COPY_MAX_PRICE, COPY_MIN_PRICE,
    COPY_POLL_INTERVAL, COPY_LOOKBACK_SECONDS,
    COPY_MAX_OPEN_POSITIONS, COPY_MAX_DAILY_SPEND,
)
from trade_mirror import TradeMonitor, TradeMirror

shutdown_event = asyncio.Event()


def print_config():
    """Print current configuration."""
    print("=" * 55, flush=True)
    print("  POLYMARKET COPY-TRADE BOT", flush=True)
    print("=" * 55, flush=True)
    print(f"  Targets:          {len(COPY_TARGETS)} wallet(s)", flush=True)
    for i, t in enumerate(COPY_TARGETS):
        print(f"    [{i+1}] {t}", flush=True)
    print(f"  Size per trade:   ${COPY_SIZE_USD:.2f}", flush=True)
    print(f"  Price range:      {COPY_MIN_PRICE} - {COPY_MAX_PRICE}", flush=True)
    print(f"  Poll interval:    {COPY_POLL_INTERVAL}s", flush=True)
    print(f"  Lookback:         {COPY_LOOKBACK_SECONDS}s", flush=True)
    print(f"  Max positions:    {COPY_MAX_OPEN_POSITIONS}", flush=True)
    print(f"  Max daily spend:  ${COPY_MAX_DAILY_SPEND:.2f}", flush=True)
    print(f"  Funder:           {POLY_FUNDER}", flush=True)
    print("=" * 55, flush=True)


def init_clob_client():
    """Derive API credentials and create an authenticated ClobClient."""
    print("\n  [INIT] Deriving API credentials...", flush=True)

    l1_client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=POLY_PRIVATE_KEY,
        signature_type=2,  # POLY_GNOSIS_SAFE
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

    print(f"  [INIT] API Key: {api_key[:20]}...", flush=True)

    client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=POLY_PRIVATE_KEY,
        creds=ApiCreds(api_key, api_secret, api_passphrase),
        funder=POLY_FUNDER,
        signature_type=2,
    )

    # Verify auth
    keys_resp = client.get_api_keys()
    print(f"  [INIT] Auth verified: {keys_resp}", flush=True)

    return client


async def run():
    """Main polling loop."""
    if not COPY_TARGETS:
        print("[FATAL] No COPY_TARGETS configured. Set in .env or Railway vars.", flush=True)
        sys.exit(1)

    if not POLY_PRIVATE_KEY:
        print("[FATAL] POLY_PRIVATE_KEY not set.", flush=True)
        sys.exit(1)

    print_config()

    # Initialize CLOB client
    client = init_clob_client()

    # Set up monitors and mirror
    monitors = {}
    for addr in COPY_TARGETS:
        monitors[addr] = TradeMonitor(addr, COPY_LOOKBACK_SECONDS)
        print(f"  [INIT] Monitoring: {addr}", flush=True)

    mirror = TradeMirror(
        clob_client=client,
        size_usd=COPY_SIZE_USD,
        max_price=COPY_MAX_PRICE,
        min_price=COPY_MIN_PRICE,
        max_open_positions=COPY_MAX_OPEN_POSITIONS,
        max_daily_spend=COPY_MAX_DAILY_SPEND,
    )

    # Rebuild our current positions
    async with aiohttp.ClientSession() as session:
        await mirror.rebuild_positions(session, POLY_FUNDER)

    print(f"\n  [LIVE] Polling every {COPY_POLL_INTERVAL}s...\n", flush=True)

    # Main loop
    async with aiohttp.ClientSession() as session:
        poll_count = 0
        while not shutdown_event.is_set():
            poll_count += 1

            for addr, monitor in monitors.items():
                try:
                    new_trades = await monitor.poll(session)
                    for trade in new_trades:
                        side = trade.get("side", "?")
                        outcome = trade.get("outcome", "?")
                        title = (trade.get("title") or "?")[:30]
                        print(f"\n  [NEW] {addr[:10]}... {side} {outcome} | {title}", flush=True)

                        result = await mirror.mirror_trade(trade)

                except Exception as e:
                    print(f"  [ERROR] Polling {addr[:10]}...: {e}", flush=True)
                    if poll_count <= 3:
                        traceback.print_exc()

            # Periodic status (every 60 polls)
            if poll_count % 60 == 0:
                pos_count = len(mirror.open_positions)
                print(f"  [STATUS] Poll #{poll_count} | "
                      f"Positions: {pos_count} | "
                      f"Daily spent: ${mirror.daily_spent:.2f}", flush=True)

            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=COPY_POLL_INTERVAL,
                )
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # normal — poll again

    print("\n  [SHUTDOWN] Copy-trade bot stopped.", flush=True)


def handle_signal(sig, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    print(f"\n  [SIGNAL] Received {signal.Signals(sig).name}, shutting down...", flush=True)
    shutdown_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
    except Exception as e:
        print(f"\n[FATAL] {e}", flush=True)
        traceback.print_exc()
    sys.exit(0)
