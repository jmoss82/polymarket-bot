"""
Trade detection and mirroring logic for the copy-trade bot.

TradeMonitor — polls a target wallet's trades, detects new ones.
TradeMirror  — validates and executes copy trades via the CLOB.
"""
import math
import time
import asyncio
import aiohttp
from datetime import datetime, timezone

from py_clob_client.clob_types import OrderArgs, BalanceAllowanceParams, AssetType

DATA_API = "https://data-api.polymarket.com"


class TradeMonitor:
    """Polls the Data API for new trades from a target wallet."""

    def __init__(self, target_wallet, lookback_seconds=30):
        self.target = target_wallet
        self.lookback_seconds = lookback_seconds
        self.seen_tx_hashes = set()
        self.start_time = time.time()

    async def poll(self, session):
        """
        Fetch recent trades and return only new ones.
        Returns list of trade dicts: {side, size, price, asset, conditionId,
        outcome, outcomeIndex, title, transactionHash, timestamp}
        """
        url = f"{DATA_API}/trades"
        params = {"user": self.target, "limit": 50}

        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            trades = await resp.json()
            if not isinstance(trades, list):
                return []

        new_trades = []
        for t in trades:
            tx_hash = t.get("transactionHash")
            if not tx_hash or tx_hash in self.seen_tx_hashes:
                continue

            # Parse timestamp — Data API returns unix seconds as string
            ts = t.get("timestamp")
            if ts:
                try:
                    trade_ts = float(ts)
                except (ValueError, TypeError):
                    continue
                # Skip trades older than lookback window or before bot started
                cutoff = max(self.start_time, time.time() - self.lookback_seconds)
                if trade_ts < cutoff:
                    self.seen_tx_hashes.add(tx_hash)
                    continue

            self.seen_tx_hashes.add(tx_hash)
            new_trades.append(t)

        return new_trades


class TradeMirror:
    """Executes copy trades via the CLOB client."""

    def __init__(self, clob_client, size_usd, max_price, min_price,
                 max_open_positions, max_daily_spend):
        self.client = clob_client
        self.size_usd = size_usd
        self.max_price = max_price
        self.min_price = min_price
        self.max_open_positions = max_open_positions
        self.max_daily_spend = max_daily_spend

        # Track our positions: asset (token_id) -> {conditionId, outcome, title, ...}
        self.open_positions = {}
        # Daily spend tracking
        self.daily_spent = 0.0
        self.daily_reset_date = ""

    def _reset_daily_if_needed(self):
        """Reset daily spend counter at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.daily_reset_date:
            self.daily_reset_date = today
            self.daily_spent = 0.0

    async def rebuild_positions(self, session, our_wallet):
        """Rebuild open positions from our wallet's current state."""
        url = f"{DATA_API}/positions"
        params = {"user": our_wallet, "limit": 500, "sizeThreshold": 0}
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
                positions = data if isinstance(data, list) else data.get("positions", [])

            self.open_positions = {}
            for p in positions:
                asset = p.get("asset")
                size = float(p.get("size", 0))
                if asset and size > 0.01:  # ignore dust
                    self.open_positions[asset] = {
                        "conditionId": p.get("conditionId"),
                        "outcome": p.get("outcome"),
                        "title": p.get("title"),
                        "size": size,
                        "avgPrice": float(p.get("avgPrice", 0)),
                    }
            print(f"  [INIT] Rebuilt {len(self.open_positions)} open positions", flush=True)
        except Exception as e:
            print(f"  [WARN] Failed to rebuild positions: {e}", flush=True)

    async def mirror_trade(self, trade):
        """
        Mirror a detected trade. Returns order result or None if skipped.
        """
        side = trade.get("side", "").upper()
        asset = trade.get("asset")  # token_id
        price = float(trade.get("price", 0))
        condition_id = trade.get("conditionId")
        outcome = trade.get("outcome", "?")
        title = trade.get("title", "?")
        tx_hash = trade.get("transactionHash", "?")

        tag = f"[{title[:30]}|{outcome}]"

        if side == "BUY":
            return await self._mirror_buy(asset, price, condition_id, outcome, title, tag)
        elif side == "SELL":
            return await self._mirror_sell(asset, condition_id, outcome, title, tag)
        else:
            print(f"  {tag} Unknown side '{side}', skipping", flush=True)
            return None

    async def _mirror_buy(self, asset, target_price, condition_id, outcome, title, tag):
        """Mirror a BUY trade."""
        self._reset_daily_if_needed()

        # Skip if we already hold this asset (no doubling up)
        if asset in self.open_positions:
            print(f"  {tag} Already holding, skip BUY", flush=True)
            return None

        # Price bounds check
        if target_price > self.max_price:
            print(f"  {tag} Price {target_price:.3f} > max {self.max_price}, skip", flush=True)
            return None
        if target_price < self.min_price:
            print(f"  {tag} Price {target_price:.3f} < min {self.min_price}, skip", flush=True)
            return None

        # Position limit check
        if len(self.open_positions) >= self.max_open_positions:
            print(f"  {tag} At max positions ({self.max_open_positions}), skip", flush=True)
            return None

        # Daily spend check
        if self.daily_spent + self.size_usd > self.max_daily_spend:
            print(f"  {tag} Daily spend cap (${self.max_daily_spend}) reached, skip", flush=True)
            return None

        # Calculate size — ensure > 5 shares for sellability
        order_price = min(round(target_price + 0.01, 2), 0.99)
        raw_size = self.size_usd / order_price
        size = math.floor(raw_size * 100) / 100

        if size < 5.0:
            # Bump to ensure we can sell later
            size = 5.0
            effective_cost = size * order_price
            if effective_cost > self.max_daily_spend - self.daily_spent:
                print(f"  {tag} Can't meet 5-share min within daily cap, skip", flush=True)
                return None

        # Check $1 minimum order value
        if size * order_price < 1.0:
            print(f"  {tag} Order value ${size * order_price:.2f} < $1 min, skip", flush=True)
            return None

        print(f"  {tag} BUY {size} shares @ {order_price} (~${size * order_price:.2f})", flush=True)

        try:
            result = self.client.create_and_post_order(
                OrderArgs(
                    token_id=asset,
                    price=order_price,
                    size=size,
                    side="BUY",
                )
            )

            order_id = None
            if isinstance(result, dict):
                order_id = result.get("orderID")
                error_msg = result.get("errorMsg") or result.get("error")
                if result.get("success") is False:
                    print(f"  {tag} BUY REJECTED: {error_msg}", flush=True)
                    return result

            if order_id:
                self.open_positions[asset] = {
                    "conditionId": condition_id,
                    "outcome": outcome,
                    "title": title,
                    "size": size,
                    "avgPrice": order_price,
                }
                self.daily_spent += size * order_price
                print(f"  {tag} BUY PLACED: {order_id}", flush=True)

                # Schedule cancel check after 10s
                asyncio.get_running_loop().call_later(
                    10, lambda oid=order_id, t=tag: self._schedule_cancel(oid, t)
                )
            else:
                print(f"  {tag} BUY response: {result}", flush=True)

            return result

        except Exception as e:
            print(f"  {tag} BUY ERROR: {e}", flush=True)
            return None

    async def _mirror_sell(self, asset, condition_id, outcome, title, tag):
        """Mirror a SELL trade — sell our entire position."""
        # Only sell if we actually hold this asset
        if asset not in self.open_positions:
            print(f"  {tag} Not holding this asset, skip SELL", flush=True)
            return None

        # Get actual balance from CLOB (fee-adjusted)
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=asset,
            )
            bal_resp = self.client.get_balance_allowance(params)
            if isinstance(bal_resp, dict):
                raw_balance = float(bal_resp.get("balance", "0")) / 1e6
            else:
                raw_balance = self.open_positions[asset]["size"]
        except Exception as e:
            print(f"  {tag} Balance check failed: {e}, using tracked size", flush=True)
            raw_balance = self.open_positions[asset]["size"]

        sell_size = math.floor(raw_balance * 100) / 100

        # 5-share minimum
        if sell_size < 5.0:
            print(f"  {tag} Only {sell_size} shares (< 5 min), treating as dust", flush=True)
            self.open_positions.pop(asset, None)
            return None

        # Refresh allowance for this token before selling
        try:
            self.client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=asset,
                )
            )
        except Exception as e:
            print(f"  {tag} Allowance refresh failed: {e}", flush=True)

        # Aggressive sell price — below best bid to fill fast
        # Use a low price to ensure fill (GTC at 0.01 would fill at best bid)
        order_price = 0.01  # aggressive — will fill at best available bid

        print(f"  {tag} SELL {sell_size} shares (aggressive GTC)", flush=True)

        try:
            result = self.client.create_and_post_order(
                OrderArgs(
                    token_id=asset,
                    price=order_price,
                    size=sell_size,
                    side="SELL",
                )
            )

            order_id = None
            if isinstance(result, dict):
                order_id = result.get("orderID")
                error_msg = result.get("errorMsg") or result.get("error")
                if result.get("success") is False:
                    print(f"  {tag} SELL REJECTED: {error_msg}", flush=True)
                    return result

            if order_id:
                self.open_positions.pop(asset, None)
                print(f"  {tag} SELL PLACED: {order_id}", flush=True)
            else:
                print(f"  {tag} SELL response: {result}", flush=True)

            return result

        except Exception as e:
            print(f"  {tag} SELL ERROR: {e}", flush=True)
            return None

    def _schedule_cancel(self, order_id, tag):
        """Schedule an order cancellation check (called via call_later)."""
        asyncio.ensure_future(self._cancel_if_open(order_id, tag))

    async def _cancel_if_open(self, order_id, tag):
        """Cancel an order if it's still open after timeout."""
        try:
            order = self.client.get_order(order_id)
            if isinstance(order, dict):
                status = order.get("status", "")
                if status in ("LIVE", "OPEN"):
                    self.client.cancel(order_id)
                    print(f"  {tag} Cancelled unfilled order {order_id}", flush=True)
        except Exception as e:
            print(f"  {tag} Cancel check failed: {e}", flush=True)
