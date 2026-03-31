"""
Polymarket wallet analyzer — fetch and score leaderboard wallets.
Uses the public Data API (no auth required).
"""
import asyncio
import aiohttp
import time

DATA_API = "https://data-api.polymarket.com"


async def fetch_leaderboard(session, category="CRYPTO", time_period="WEEK",
                            order_by="PNL", limit=50, offset=0):
    """Fetch leaderboard rankings."""
    url = f"{DATA_API}/v1/leaderboard"
    params = {
        "category": category,
        "timePeriod": time_period,
        "orderBy": order_by,
        "limit": limit,
        "offset": offset,
    }
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_positions(session, proxy_wallet, limit=500):
    """Fetch a wallet's current open positions."""
    url = f"{DATA_API}/positions"
    params = {"user": proxy_wallet, "limit": limit, "sizeThreshold": 0}
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data if isinstance(data, list) else data.get("positions", [])


async def fetch_closed_positions(session, proxy_wallet, limit=500, offset=0):
    """Fetch a wallet's resolved/closed positions."""
    url = f"{DATA_API}/closed-positions"
    params = {"user": proxy_wallet, "limit": limit, "offset": offset}
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data if isinstance(data, list) else []


async def fetch_all_closed_positions(session, proxy_wallet):
    """Page through all closed positions for a wallet."""
    all_positions = []
    offset = 0
    limit = 500
    while True:
        batch = await fetch_closed_positions(session, proxy_wallet, limit=limit, offset=offset)
        all_positions.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        await asyncio.sleep(0.2)
    return all_positions


async def fetch_trades(session, proxy_wallet, limit=100):
    """Fetch a wallet's recent trades."""
    url = f"{DATA_API}/trades"
    params = {"user": proxy_wallet, "limit": limit}
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data if isinstance(data, list) else []


async def fetch_portfolio_value(session, proxy_wallet):
    """Fetch a wallet's current portfolio value in USD."""
    url = f"{DATA_API}/value"
    params = {"user": proxy_wallet}
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
        if data and isinstance(data, list) and len(data) > 0:
            return float(data[0].get("value", 0))
        return 0.0


async def fetch_wallet_profile(session, proxy_wallet):
    """Fetch full profile for a single wallet: positions, closed, trades, value."""
    closed, positions, trades, value = await asyncio.gather(
        fetch_all_closed_positions(session, proxy_wallet),
        fetch_positions(session, proxy_wallet),
        fetch_trades(session, proxy_wallet, limit=500),
        fetch_portfolio_value(session, proxy_wallet),
    )
    return {
        "proxy_wallet": proxy_wallet,
        "open_positions": positions,
        "closed_positions": closed,
        "recent_trades": trades,
        "portfolio_value": value,
    }


def score_wallet(profile):
    """
    Score a wallet based on closed positions and trading activity.
    Returns a dict of metrics.
    """
    closed = profile.get("closed_positions", [])
    open_pos = profile.get("open_positions", [])
    trades = profile.get("recent_trades", [])

    total_resolved = len(closed)
    wins = sum(1 for p in closed if float(p.get("realizedPnl", 0)) > 0)
    losses = sum(1 for p in closed if float(p.get("realizedPnl", 0)) < 0)
    breakeven = total_resolved - wins - losses

    win_rate = wins / total_resolved if total_resolved > 0 else 0.0

    realized_pnls = [float(p.get("realizedPnl", 0)) for p in closed]
    total_pnl = sum(realized_pnls)
    avg_pnl = total_pnl / total_resolved if total_resolved > 0 else 0.0

    avg_win = 0.0
    avg_loss = 0.0
    if wins > 0:
        avg_win = sum(p for p in realized_pnls if p > 0) / wins
    if losses > 0:
        avg_loss = sum(p for p in realized_pnls if p < 0) / losses

    # Trade frequency — trades per day over last 30 days
    now = time.time()
    recent_cutoff = now - (30 * 86400)
    recent_trades = []
    for t in trades:
        ts = t.get("timestamp")
        if ts:
            # Handle both unix seconds and ISO format
            try:
                trade_ts = float(ts)
            except (ValueError, TypeError):
                continue
            if trade_ts > recent_cutoff:
                recent_trades.append(t)
    trades_per_day = len(recent_trades) / 30.0

    # Distinct markets traded
    market_ids = set()
    for t in trades:
        cid = t.get("conditionId")
        if cid:
            market_ids.add(cid)

    # Crypto focus — check titles for crypto keywords
    crypto_keywords = {"btc", "bitcoin", "eth", "ethereum", "sol", "solana",
                       "xrp", "ripple", "crypto", "doge", "dogecoin", "ada",
                       "cardano", "bnb", "avax", "matic", "polygon", "link",
                       "chainlink", "dot", "polkadot", "uni", "uniswap",
                       "ltc", "litecoin", "shib", "pepe", "bonk", "sui",
                       "apt", "aptos", "ton", "near", "arb", "arbitrum",
                       "op", "optimism", "meme", "memecoin"}
    crypto_positions = 0
    for p in closed:
        title = (p.get("title") or "").lower()
        if any(kw in title for kw in crypto_keywords):
            crypto_positions += 1
    crypto_ratio = crypto_positions / total_resolved if total_resolved > 0 else 0.0

    return {
        "proxy_wallet": profile["proxy_wallet"],
        "total_resolved": total_resolved,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "open_position_count": len(open_pos),
        "portfolio_value": profile.get("portfolio_value", 0),
        "trades_per_day": trades_per_day,
        "distinct_markets": len(market_ids),
        "crypto_ratio": crypto_ratio,
        "crypto_positions": crypto_positions,
    }


def rank_wallets(scored_wallets, min_win_rate=0.0, min_positions=0):
    """Filter and rank wallets by win rate, then total PnL."""
    filtered = [
        w for w in scored_wallets
        if w["win_rate"] >= min_win_rate and w["total_resolved"] >= min_positions
    ]
    return sorted(filtered, key=lambda w: (w["win_rate"], w["total_pnl"]), reverse=True)
