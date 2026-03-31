"""
Find positions held by multiple top traders (consensus trades).

Reads open positions from the Data API for leaderboard wallets and
groups by market + outcome to find overlap.

Usage:
    python find_overlaps.py                        # top 25 wallets, min 2 overlap
    python find_overlaps.py --top 50 --min 3       # top 50 wallets, min 3 overlap
    python find_overlaps.py --top 10 --min 2       # top 10, min 2
"""
import asyncio
import aiohttp
import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from wallet_analyzer import fetch_positions, fetch_leaderboard

DATA_DIR = "data"


async def run(args):
    async with aiohttp.ClientSession() as session:
        # Fetch leaderboard
        print(f"\n  Fetching top {args.top} crypto leaderboard...", flush=True)
        leaderboard = await fetch_leaderboard(
            session,
            category="CRYPTO",
            time_period=args.time_period,
            order_by="PNL",
            limit=args.top,
        )

        if not leaderboard:
            print("  No leaderboard data.", flush=True)
            return

        print(f"  Found {len(leaderboard)} wallets. Fetching positions...\n", flush=True)

        # Fetch all positions
        # market key -> list of {wallet, userName, outcome, size, avgPrice, curPrice, cashPnl, ...}
        market_positions = defaultdict(list)
        wallet_names = {}

        for i, entry in enumerate(leaderboard):
            wallet = entry.get("proxyWallet")
            name = entry.get("userName", "")
            rank = int(entry.get("rank", i + 1))
            wallet_names[wallet] = name or wallet[:16]

            try:
                positions = await fetch_positions(session, wallet, limit=500)
                for p in positions:
                    title = p.get("title") or "?"
                    outcome = p.get("outcome") or "?"
                    condition_id = p.get("conditionId") or ""
                    key = (condition_id, title, outcome)

                    market_positions[key].append({
                        "wallet": wallet,
                        "userName": wallet_names[wallet],
                        "rank": rank,
                        "size": float(p.get("size", 0)),
                        "avgPrice": float(p.get("avgPrice", 0)),
                        "curPrice": float(p.get("curPrice", 0)),
                        "currentValue": float(p.get("currentValue", 0)),
                        "cashPnl": float(p.get("cashPnl", 0)),
                        "percentPnl": float(p.get("percentPnl", 0)),
                        "conditionId": condition_id,
                        "asset": p.get("asset", ""),
                        "slug": p.get("slug", ""),
                        "endDate": p.get("endDate", ""),
                    })

                sys.stdout.write(f"\r  Fetched {i + 1}/{len(leaderboard)}: {wallet_names[wallet]:<20}")
                sys.stdout.flush()
            except Exception as e:
                print(f"\n  [WARN] {wallet[:16]}: {e}", flush=True)

            await asyncio.sleep(0.2)

        print("\n", flush=True)

        # Filter out expired positions (endDate before today)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filtered_positions = {}
        skipped = 0
        for key, holders in market_positions.items():
            end_date = holders[0].get("endDate", "")
            # Skip short-duration markets (under 15 min)
            # Titles look like "Bitcoin Up or Down - March 31, 2:15AM-2:20AM ET"
            # Parse the time range and calculate duration
            title = key[1]
            time_range = re.search(r'(\d{1,2}(?::\d{2})?)(AM|PM)\s*-\s*(\d{1,2}(?::\d{2})?)(AM|PM)', title, re.IGNORECASE)
            if time_range:
                def parse_minutes(t, ampm):
                    parts = t.split(":")
                    h = int(parts[0])
                    m = int(parts[1]) if len(parts) > 1 else 0
                    if ampm.upper() == "PM" and h != 12:
                        h += 12
                    if ampm.upper() == "AM" and h == 12:
                        h = 0
                    return h * 60 + m

                start_min = parse_minutes(time_range.group(1), time_range.group(2))
                end_min = parse_minutes(time_range.group(3), time_range.group(4))
                duration = end_min - start_min
                if duration < 0:
                    duration += 24 * 60  # crosses midnight
                if duration < 15:
                    skipped += len(holders)
                    continue

            # Skip if end date is in the past
            if end_date:
                end_day = end_date[:10]
                if end_day < now_str:
                    skipped += len(holders)
                    continue

            # Skip if market has already resolved (price snapped to 0 or 1)
            cur_price = holders[0].get("curPrice", 0)
            try:
                cp = float(cur_price)
                if cp <= 0.005 or cp >= 0.995:
                    skipped += len(holders)
                    continue
            except (ValueError, TypeError):
                pass

            filtered_positions[key] = holders

        if skipped:
            print(f"  Filtered out {skipped} positions from expired markets (before {now_str})", flush=True)

        # Filter to overlaps
        overlaps = []
        for (condition_id, title, outcome), holders in filtered_positions.items():
            if len(holders) >= args.min:
                total_value = sum(h["currentValue"] for h in holders)
                avg_entry = sum(h["avgPrice"] for h in holders) / len(holders)
                cur_price = holders[0]["curPrice"]
                overlaps.append({
                    "title": title,
                    "outcome": outcome,
                    "holders": len(holders),
                    "total_value": total_value,
                    "avg_entry_price": avg_entry,
                    "cur_price": cur_price,
                    "conditionId": condition_id,
                    "slug": holders[0].get("slug", ""),
                    "endDate": holders[0].get("endDate", ""),
                    "trader_details": sorted(holders, key=lambda h: h["rank"]),
                })

        overlaps.sort(key=lambda x: x["holders"], reverse=True)

        # Display
        print(f"{'=' * 80}", flush=True)
        print(f"  CONSENSUS POSITIONS  |  {len(overlaps)} markets with {args.min}+ traders", flush=True)
        print(f"{'=' * 80}", flush=True)

        if not overlaps:
            print(f"\n  No positions with {args.min}+ traders overlapping.", flush=True)
            print(f"  Try lowering --min or raising --top.", flush=True)
            return

        for o in overlaps:
            print(f"\n  [{o['holders']} traders]  {o['title']}", flush=True)
            print(f"  Side: {o['outcome']}  |  Cur: {o['cur_price']:.3f}  |  "
                  f"Avg entry: {o['avg_entry_price']:.3f}  |  "
                  f"Combined value: ${o['total_value']:,.2f}", flush=True)
            if o["endDate"]:
                print(f"  End date: {o['endDate']}", flush=True)
            for h in o["trader_details"]:
                print(f"    #{h['rank']:<4} {h['userName']:<20} "
                      f"{h['size']:>10.2f} shares  @ {h['avgPrice']:.3f}  "
                      f"P&L: ${h['cashPnl']:>8,.2f} ({h['percentPnl']:>6.1%})", flush=True)

        # Save CSV
        os.makedirs(DATA_DIR, exist_ok=True)
        csv_path = os.path.join(DATA_DIR, "consensus_positions.csv")
        csv_rows = []
        for o in overlaps:
            for h in o["trader_details"]:
                csv_rows.append({
                    "title": o["title"],
                    "outcome": o["outcome"],
                    "num_holders": o["holders"],
                    "cur_price": o["cur_price"],
                    "end_date": o["endDate"],
                    "conditionId": o["conditionId"],
                    "wallet": h["wallet"],
                    "userName": h["userName"],
                    "rank": h["rank"],
                    "size": h["size"],
                    "avgPrice": h["avgPrice"],
                    "currentValue": h["currentValue"],
                    "cashPnl": h["cashPnl"],
                    "percentPnl": h["percentPnl"],
                    "asset": h["asset"],
                    "slug": o["slug"],
                })

        if csv_rows:
            fields = list(csv_rows[0].keys())
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(csv_rows)
            print(f"\n  Saved {len(csv_rows)} rows to {csv_path}", flush=True)

        # Summary
        print(f"\n  {'=' * 50}", flush=True)
        print(f"  SUMMARY", flush=True)
        print(f"  {'=' * 50}", flush=True)
        print(f"  Markets with {args.min}+ traders: {len(overlaps)}", flush=True)
        if overlaps:
            max_overlap = overlaps[0]["holders"]
            print(f"  Highest overlap: {max_overlap} traders", flush=True)
            top5 = overlaps[:5]
            print(f"\n  Top 5 consensus positions:", flush=True)
            for o in top5:
                print(f"    [{o['holders']}] {o['outcome']}: {o['title'][:60]}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Find consensus positions among top Polymarket traders")
    parser.add_argument("--top", type=int, default=25, help="Number of leaderboard wallets to check (default: 25)")
    parser.add_argument("--min", type=int, default=2, help="Minimum traders holding same position (default: 2)")
    parser.add_argument("--time-period", default="WEEK", choices=["DAY", "WEEK", "MONTH", "ALL"],
                        help="Leaderboard time period (default: WEEK)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
