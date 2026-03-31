"""
View open positions for one or more wallets.

Usage:
    python open_positions.py 0xabc123...                    # single wallet
    python open_positions.py 0xabc123... 0xdef456...        # multiple wallets
    python open_positions.py --from-rankings                # all wallets from last discovery run
    python open_positions.py --from-rankings --top 5        # top 5 from last run
"""
import asyncio
import aiohttp
import argparse
import csv
import json
import os
import sys

from wallet_analyzer import fetch_positions, DATA_API

DATA_DIR = "data"


def print_positions(wallet, positions, label=""):
    header = f"  {label}  " if label else ""
    print(f"\n{'=' * 80}", flush=True)
    print(f"  {header}{wallet}", flush=True)
    print(f"  {len(positions)} open position(s)", flush=True)
    print(f"{'=' * 80}", flush=True)

    if not positions:
        print("  (none)", flush=True)
        return

    print(f"  {'Title':<45} {'Side':<6} {'Size':>8} {'AvgPx':>7} {'CurPx':>7} {'P&L':>10} {'P&L%':>8}", flush=True)
    print(f"  {'-' * 45} {'-' * 6} {'-' * 8} {'-' * 7} {'-' * 7} {'-' * 10} {'-' * 8}", flush=True)

    for p in sorted(positions, key=lambda x: abs(float(x.get("currentValue", 0))), reverse=True):
        title = (p.get("title") or "")[:45]
        outcome = p.get("outcome", "?")
        size = float(p.get("size", 0))
        avg_price = float(p.get("avgPrice", 0))
        cur_price = float(p.get("curPrice", 0))
        cash_pnl = float(p.get("cashPnl", 0))
        pct_pnl = float(p.get("percentPnl", 0))
        print(f"  {title:<45} {outcome:<6} {size:>8.2f} {avg_price:>7.3f} {cur_price:>7.3f} ${cash_pnl:>8,.2f} {pct_pnl:>7.1%}", flush=True)

    total_value = sum(float(p.get("currentValue", 0)) for p in positions)
    total_pnl = sum(float(p.get("cashPnl", 0)) for p in positions)
    print(f"\n  Total value: ${total_value:,.2f}  |  Total P&L: ${total_pnl:,.2f}", flush=True)


async def run(args):
    wallets = []

    if args.from_rankings:
        json_path = os.path.join(DATA_DIR, "wallet_rankings.json")
        if not os.path.exists(json_path):
            print(f"  No rankings file found at {json_path}. Run discover_wallets.py first.", flush=True)
            return
        with open(json_path) as f:
            rankings = json.load(f)
        top_n = args.top or len(rankings)
        wallets = [(r.get("proxy_wallet"), r.get("userName", "")) for r in rankings[:top_n]]
    else:
        wallets = [(addr, "") for addr in args.wallets]

    if not wallets:
        print("  No wallets specified. Pass addresses or use --from-rankings.", flush=True)
        return

    all_rows = []

    async with aiohttp.ClientSession() as session:
        for wallet, name in wallets:
            label = name if name else ""
            try:
                positions = await fetch_positions(session, wallet, limit=500)
                print_positions(wallet, positions, label=label)

                for p in positions:
                    all_rows.append({
                        "wallet": wallet,
                        "userName": name,
                        "title": p.get("title", ""),
                        "outcome": p.get("outcome", ""),
                        "size": float(p.get("size", 0)),
                        "avgPrice": float(p.get("avgPrice", 0)),
                        "curPrice": float(p.get("curPrice", 0)),
                        "currentValue": float(p.get("currentValue", 0)),
                        "initialValue": float(p.get("initialValue", 0)),
                        "cashPnl": float(p.get("cashPnl", 0)),
                        "percentPnl": float(p.get("percentPnl", 0)),
                        "conditionId": p.get("conditionId", ""),
                        "asset": p.get("asset", ""),
                        "endDate": p.get("endDate", ""),
                        "slug": p.get("slug", ""),
                    })
            except Exception as e:
                print(f"\n  [ERROR] {wallet[:16]}: {e}", flush=True)

            await asyncio.sleep(0.2)

    # Save CSV
    if all_rows:
        os.makedirs(DATA_DIR, exist_ok=True)
        csv_path = os.path.join(DATA_DIR, "open_positions.csv")
        fields = list(all_rows[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\n  Saved {len(all_rows)} positions to {csv_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="View open positions for Polymarket wallets")
    parser.add_argument("wallets", nargs="*", help="Proxy wallet address(es)")
    parser.add_argument("--from-rankings", action="store_true",
                        help="Use wallets from last discover_wallets.py run")
    parser.add_argument("--top", type=int, default=None,
                        help="Only check top N wallets from rankings")
    args = parser.parse_args()

    if not args.wallets and not args.from_rankings:
        parser.print_help()
        return

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
