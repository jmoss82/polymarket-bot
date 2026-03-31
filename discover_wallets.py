"""
Discover and rank profitable Polymarket wallets from the crypto leaderboard.

Usage:
    python discover_wallets.py                              # top 50, default filters
    python discover_wallets.py --min-win-rate 0.65          # stricter win rate
    python discover_wallets.py --min-positions 20           # more sample size
    python discover_wallets.py --time-period ALL            # all-time leaderboard
    python discover_wallets.py --detail 0xabc123...         # deep dive one wallet
"""
import asyncio
import aiohttp
import argparse
import json
import os
import sys

from wallet_analyzer import (
    fetch_leaderboard,
    fetch_wallet_profile,
    score_wallet,
    rank_wallets,
)

DATA_DIR = "data"


def print_header(title):
    print(f"\n{'=' * 70}", flush=True)
    print(f"  {title}", flush=True)
    print(f"{'=' * 70}", flush=True)


def print_ranked_table(ranked):
    """Print a formatted table of ranked wallets."""
    if not ranked:
        print("\n  No wallets match your filters.", flush=True)
        return

    print(f"\n  {'#':>3}  {'Win%':>6}  {'W/L':>7}  {'Resolved':>8}  "
          f"{'Total PnL':>11}  {'Avg PnL':>9}  {'Crypto%':>7}  {'Wallet'}", flush=True)
    print(f"  {'-' * 3}  {'-' * 6}  {'-' * 7}  {'-' * 8}  "
          f"{'-' * 11}  {'-' * 9}  {'-' * 7}  {'-' * 42}", flush=True)

    for i, w in enumerate(ranked, 1):
        wl = f"{w['wins']}/{w['losses']}"
        print(f"  {i:>3}  {w['win_rate']:>5.1%}  {wl:>7}  {w['total_resolved']:>8}  "
              f"  ${w['total_pnl']:>9,.2f}  ${w['avg_pnl']:>7,.2f}  {w['crypto_ratio']:>6.0%}  "
              f"  {w['proxy_wallet'][:20]}...", flush=True)


def print_wallet_detail(score, profile):
    """Print detailed breakdown for a single wallet."""
    print(f"\n  Wallet:          {score['proxy_wallet']}", flush=True)
    print(f"  Portfolio Value: ${score['portfolio_value']:,.2f}", flush=True)
    print(f"  Win Rate:        {score['win_rate']:.1%}  ({score['wins']}W / {score['losses']}L / {score['breakeven']}BE)", flush=True)
    print(f"  Total Resolved:  {score['total_resolved']}", flush=True)
    print(f"  Total PnL:       ${score['total_pnl']:,.2f}", flush=True)
    print(f"  Avg PnL/Trade:   ${score['avg_pnl']:,.2f}", flush=True)
    print(f"  Avg Win:         ${score['avg_win']:,.2f}", flush=True)
    print(f"  Avg Loss:        ${score['avg_loss']:,.2f}", flush=True)
    print(f"  Trades/Day:      {score['trades_per_day']:.1f}", flush=True)
    print(f"  Distinct Markets:{score['distinct_markets']}", flush=True)
    print(f"  Crypto Ratio:    {score['crypto_ratio']:.0%} ({score['crypto_positions']} positions)", flush=True)
    print(f"  Open Positions:  {score['open_position_count']}", flush=True)

    # Show open positions
    open_pos = profile.get("open_positions", [])
    if open_pos:
        print(f"\n  Open Positions ({len(open_pos)}):", flush=True)
        print(f"  {'Title':<45} {'Side':<6} {'Size':>8} {'AvgPx':>7} {'CurPx':>7} {'P&L':>10} {'P&L%':>7}", flush=True)
        print(f"  {'-' * 45} {'-' * 6} {'-' * 8} {'-' * 7} {'-' * 7} {'-' * 10} {'-' * 7}", flush=True)
        for p in sorted(open_pos, key=lambda x: abs(float(x.get("cashPnl", 0))), reverse=True):
            title = (p.get("title") or "")[:45]
            outcome = p.get("outcome", "?")
            size = float(p.get("size", 0))
            avg_price = float(p.get("avgPrice", 0))
            cur_price = float(p.get("curPrice", 0))
            cash_pnl = float(p.get("cashPnl", 0))
            pct_pnl = float(p.get("percentPnl", 0))
            print(f"  {title:<45} {outcome:<6} {size:>8.2f} {avg_price:>7.3f} {cur_price:>7.3f} ${cash_pnl:>8,.2f} {pct_pnl:>6.1%}", flush=True)

    # Show recent closed positions
    closed = profile.get("closed_positions", [])
    if closed:
        recent_closed = sorted(closed, key=lambda x: x.get("timestamp", ""), reverse=True)[:15]
        print(f"\n  Recent Closed Positions ({len(closed)} total, showing last 15):", flush=True)
        print(f"  {'Title':<50} {'Side':<6} {'AvgPx':>7} {'PnL':>10}", flush=True)
        print(f"  {'-' * 50} {'-' * 6} {'-' * 7} {'-' * 10}", flush=True)
        for p in recent_closed:
            title = (p.get("title") or "")[:50]
            outcome = p.get("outcome", "?")
            avg_price = float(p.get("avgPrice", 0))
            pnl = float(p.get("realizedPnl", 0))
            marker = "W" if pnl > 0 else "L" if pnl < 0 else "-"
            print(f"  {title:<50} {outcome:<6} {avg_price:>7.3f} ${pnl:>8,.2f} {marker}", flush=True)


async def run_discovery(args):
    """Fetch leaderboard, score all wallets, display results."""
    async with aiohttp.ClientSession() as session:
        print_header(f"Polymarket Crypto Leaderboard — {args.time_period}")
        print(f"\n  Fetching top {args.limit} wallets...", flush=True)

        leaderboard = await fetch_leaderboard(
            session,
            category="CRYPTO",
            time_period=args.time_period,
            order_by=args.order_by,
            limit=args.limit,
        )

        if not leaderboard:
            print("  No leaderboard data returned.", flush=True)
            return

        print(f"  Found {len(leaderboard)} wallets. Analyzing each...\n", flush=True)

        scored = []
        for i, entry in enumerate(leaderboard):
            wallet = entry.get("proxyWallet")
            name = entry.get("userName", "")
            lb_pnl = float(entry.get("pnl", 0))
            lb_vol = float(entry.get("vol", 0))

            if not wallet:
                continue

            try:
                profile = await fetch_wallet_profile(session, wallet)
                sc = score_wallet(profile)
                sc["userName"] = name
                sc["lb_pnl"] = lb_pnl
                sc["lb_vol"] = lb_vol
                sc["lb_rank"] = int(entry.get("rank", i + 1))
                scored.append(sc)
                sys.stdout.write(f"\r  Analyzed {i + 1}/{len(leaderboard)}: {name or wallet[:16]}...  ")
                sys.stdout.flush()
            except Exception as e:
                print(f"\n  [WARN] Failed to analyze {wallet[:16]}: {e}", flush=True)

            await asyncio.sleep(0.2)  # rate limiting

        print("", flush=True)

        ranked = rank_wallets(scored, min_win_rate=args.min_win_rate, min_positions=args.min_positions)
        print_header(f"Ranked Results ({len(ranked)}/{len(scored)} passed filters)")
        print(f"  Filters: win_rate >= {args.min_win_rate:.0%}, resolved >= {args.min_positions}", flush=True)
        print_ranked_table(ranked)

        # Save results
        os.makedirs(DATA_DIR, exist_ok=True)
        out_path = os.path.join(DATA_DIR, "wallet_rankings.json")
        with open(out_path, "w") as f:
            json.dump(ranked, f, indent=2)
        print(f"\n  Saved {len(ranked)} results to {out_path}", flush=True)


async def run_detail(args):
    """Deep dive into a single wallet."""
    async with aiohttp.ClientSession() as session:
        wallet = args.detail
        print_header(f"Wallet Detail: {wallet}")
        print(f"\n  Fetching profile...", flush=True)

        profile = await fetch_wallet_profile(session, wallet)
        sc = score_wallet(profile)
        print_wallet_detail(sc, profile)


async def main():
    parser = argparse.ArgumentParser(description="Discover profitable Polymarket wallets")
    parser.add_argument("--limit", type=int, default=50, help="Leaderboard size (default: 50)")
    parser.add_argument("--time-period", default="WEEK", choices=["DAY", "WEEK", "MONTH", "ALL"],
                        help="Leaderboard time period (default: WEEK)")
    parser.add_argument("--order-by", default="PNL", choices=["PNL", "VOL"],
                        help="Leaderboard ordering (default: PNL)")
    parser.add_argument("--min-win-rate", type=float, default=0.55,
                        help="Minimum win rate filter (default: 0.55)")
    parser.add_argument("--min-positions", type=int, default=5,
                        help="Minimum resolved positions (default: 5)")
    parser.add_argument("--detail", type=str, default=None,
                        help="Deep dive a specific wallet address")
    args = parser.parse_args()

    if args.detail:
        await run_detail(args)
    else:
        await run_discovery(args)


if __name__ == "__main__":
    asyncio.run(main())
