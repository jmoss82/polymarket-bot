"""
Paper-test the consensus signal overnight.

Periodically scans for consensus positions among top leaderboard wallets,
records them, and tracks how they resolve over time.

Usage:
    python -u consensus_tracker.py                           # defaults
    python -u consensus_tracker.py --top 25 --min 3          # top 25 wallets, 3+ overlap
    python -u consensus_tracker.py --interval 30             # scan every 30 min
"""
import asyncio
import aiohttp
import argparse
import csv
import json
import os
import re
import sys
import signal
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from wallet_analyzer import fetch_positions, fetch_leaderboard

DATA_DIR = "data"
TRACKER_CSV = os.path.join(DATA_DIR, "consensus_tracker.csv")

shutdown_event = asyncio.Event()


def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_duration_from_title(title):
    """Parse time range from title, return duration in minutes or None."""
    match = re.search(
        r'(\d{1,2}(?::\d{2})?)(AM|PM)\s*-\s*(\d{1,2}(?::\d{2})?)(AM|PM)',
        title, re.IGNORECASE,
    )
    if not match:
        return None

    def to_minutes(t, ampm):
        parts = t.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        if ampm.upper() == "PM" and h != 12:
            h += 12
        if ampm.upper() == "AM" and h == 12:
            h = 0
        return h * 60 + m

    start = to_minutes(match.group(1), match.group(2))
    end = to_minutes(match.group(3), match.group(4))
    dur = end - start
    if dur < 0:
        dur += 24 * 60
    return dur


def load_tracked():
    """Load previously tracked positions from CSV."""
    tracked = {}
    if not os.path.exists(TRACKER_CSV):
        return tracked
    with open(TRACKER_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["conditionId"], row["outcome"])
            tracked[key] = row
    return tracked


def save_tracked(rows):
    """Save all tracked positions to CSV."""
    if not rows:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    all_rows = list(rows.values())
    fields = list(all_rows[0].keys())
    with open(TRACKER_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)


async def scan_consensus(session, top, min_overlap):
    """Run one consensus scan. Returns list of consensus positions."""
    leaderboard = await fetch_leaderboard(
        session, category="CRYPTO", time_period="WEEK",
        order_by="PNL", limit=top,
    )
    if not leaderboard:
        return []

    market_positions = defaultdict(list)
    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for i, entry in enumerate(leaderboard):
        wallet = entry.get("proxyWallet")
        name = entry.get("userName", "") or wallet[:16]
        rank = int(entry.get("rank", i + 1))

        try:
            positions = await fetch_positions(session, wallet, limit=500)
            for p in positions:
                title = p.get("title") or "?"
                outcome = p.get("outcome") or "?"
                condition_id = p.get("conditionId") or ""
                end_date = p.get("endDate", "")
                cur_price = float(p.get("curPrice", 0))

                # Skip expired
                if end_date and end_date[:10] < now_date:
                    continue
                # Skip resolved
                if cur_price <= 0.005 or cur_price >= 0.995:
                    continue
                # Skip < 15 min duration
                dur = parse_duration_from_title(title)
                if dur is not None and dur < 15:
                    continue

                key = (condition_id, title, outcome)
                market_positions[key].append({
                    "wallet": wallet,
                    "userName": name,
                    "rank": rank,
                    "size": float(p.get("size", 0)),
                    "avgPrice": float(p.get("avgPrice", 0)),
                    "curPrice": cur_price,
                    "conditionId": condition_id,
                    "asset": p.get("asset", ""),
                    "endDate": end_date,
                })
        except Exception:
            pass

        await asyncio.sleep(0.15)

    results = []
    for (condition_id, title, outcome), holders in market_positions.items():
        if len(holders) >= min_overlap:
            avg_entry = sum(h["avgPrice"] for h in holders) / len(holders)
            cur_price = holders[0]["curPrice"]
            results.append({
                "conditionId": condition_id,
                "title": title,
                "outcome": outcome,
                "num_holders": len(holders),
                "avg_entry_price": round(avg_entry, 4),
                "cur_price": round(cur_price, 4),
                "endDate": holders[0].get("endDate", ""),
                "asset": holders[0].get("asset", ""),
                "traders": ", ".join(h["userName"] for h in sorted(holders, key=lambda x: x["rank"])),
            })

    results.sort(key=lambda x: x["num_holders"], reverse=True)
    return results


GAMMA_API = "https://gamma-api.polymarket.com"


async def check_resolutions(session, tracked):
    """Check unresolved positions whose end date has passed by querying Gamma API."""
    updated = 0
    ts = now_str()
    now_iso = datetime.now(timezone.utc).isoformat()

    for key, row in list(tracked.items()):
        if row.get("resolved") == "yes":
            continue

        end_date = row.get("endDate", "")
        if not end_date or end_date > now_iso:
            continue  # not expired yet

        condition_id = row.get("conditionId", "")
        if not condition_id:
            continue

        # Query Gamma API for market resolution status
        try:
            url = f"{GAMMA_API}/markets"
            async with session.get(url, params={"id": condition_id}) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                if not data:
                    continue

                market = data[0] if isinstance(data, list) else data
                outcome_prices = market.get("outcomePrices", "")
                closed = market.get("closed")
                resolved = market.get("resolved")

                if not (closed or resolved):
                    continue

                # Parse outcome prices to determine winner
                try:
                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                except (json.JSONDecodeError, TypeError):
                    continue

                if not prices:
                    continue

                # Find which outcome won (price ~1.0)
                outcomes_raw = market.get("outcomes", "")
                try:
                    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                except (json.JSONDecodeError, TypeError):
                    outcomes = []

                winning_outcome = None
                for i, p in enumerate(prices):
                    if float(p) >= 0.99:
                        if outcomes and i < len(outcomes):
                            winning_outcome = outcomes[i]
                        break

                if winning_outcome is None:
                    # Check if all prices are 0 (voided or not yet resolved)
                    continue

                our_outcome = row.get("outcome", "")
                won = "yes" if our_outcome.lower() == winning_outcome.lower() else "no"

                row["resolved"] = "yes"
                row["resolved_at"] = ts
                row["won"] = won
                row["winning_outcome"] = winning_outcome

                result = "WIN" if won == "yes" else "LOSS"
                print(f"    [{result}] {row['title'][:50]} | Ours: {our_outcome} | Winner: {winning_outcome}", flush=True)
                updated += 1

        except Exception as e:
            pass  # skip this one, try again next scan

        await asyncio.sleep(0.2)  # rate limiting

    return updated


async def run(args):
    print(f"\n{'=' * 60}", flush=True)
    print(f"  CONSENSUS TRACKER  |  Paper testing overlap signal", flush=True)
    print(f"{'=' * 60}", flush=True)
    print(f"  Top wallets:    {args.top}", flush=True)
    print(f"  Min overlap:    {args.min}", flush=True)
    print(f"  Scan interval:  {args.interval} min (offset to mid-interval)", flush=True)
    print(f"  Tracker CSV:    {TRACKER_CSV}", flush=True)
    print(f"{'=' * 60}\n", flush=True)

    tracked = load_tracked()
    if tracked:
        resolved = sum(1 for r in tracked.values() if r.get("resolved") == "yes")
        print(f"  Loaded {len(tracked)} tracked positions ({resolved} resolved)\n", flush=True)

    scan_count = 0

    # Wait until mid-interval before first scan (e.g., :07, :22, :37, :52)
    # This avoids scanning right at the 15-min boundary where markets resolve
    now = datetime.now(timezone.utc)
    min_in_interval = now.minute % 15
    offset_target = 7  # scan at ~7 min into each 15-min interval
    if min_in_interval < offset_target:
        wait_sec = (offset_target - min_in_interval) * 60 - now.second
    else:
        wait_sec = (15 - min_in_interval + offset_target) * 60 - now.second
    if wait_sec > 0 and wait_sec < 900:
        next_scan = now + timedelta(seconds=wait_sec)
        print(f"  Waiting {wait_sec}s to align to mid-interval "
              f"(next scan ~:{(now.minute + wait_sec // 60) % 60:02d})...\n", flush=True)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=wait_sec)
            print(f"\n  Tracker stopped. Results in {TRACKER_CSV}", flush=True)
            return
        except asyncio.TimeoutError:
            pass

    async with aiohttp.ClientSession() as session:
        while not shutdown_event.is_set():
            scan_count += 1
            ts = now_str()
            print(f"  [{ts}] Scan #{scan_count}...", flush=True)

            try:
                consensus = await scan_consensus(session, args.top, args.min)
                new_count = 0

                for pos in consensus:
                    key = (pos["conditionId"], pos["outcome"])

                    if key in tracked:
                        # Update current price and holder count
                        tracked[key]["cur_price"] = str(pos["cur_price"])
                        tracked[key]["num_holders"] = str(pos["num_holders"])
                        tracked[key]["last_seen"] = ts

                        # Check if resolved
                        cp = pos["cur_price"]
                        if cp <= 0.005 or cp >= 0.995:
                            if tracked[key].get("resolved") != "yes":
                                won = "yes" if cp >= 0.995 else "no"
                                tracked[key]["resolved"] = "yes"
                                tracked[key]["resolved_at"] = ts
                                tracked[key]["won"] = won
                                result = "WIN" if won == "yes" else "LOSS"
                                print(f"    [{result}] {pos['title'][:50]} | {pos['outcome']}", flush=True)
                    else:
                        # New consensus position
                        tracked[key] = {
                            "conditionId": pos["conditionId"],
                            "title": pos["title"],
                            "outcome": pos["outcome"],
                            "num_holders": str(pos["num_holders"]),
                            "avg_entry_price": str(pos["avg_entry_price"]),
                            "price_at_discovery": str(pos["cur_price"]),
                            "cur_price": str(pos["cur_price"]),
                            "endDate": pos["endDate"],
                            "asset": pos.get("asset", ""),
                            "discovered_at": ts,
                            "last_seen": ts,
                            "traders": pos["traders"],
                            "resolved": "no",
                            "resolved_at": "",
                            "won": "",
                        }
                        new_count += 1
                        print(f"    [NEW] [{pos['num_holders']} traders] {pos['outcome']}: "
                              f"{pos['title'][:50]} @ {pos['cur_price']}", flush=True)

                # Check for resolutions on expired positions via Gamma API
                resolved_count = await check_resolutions(session, tracked)

                save_tracked(tracked)

                # Stats
                total = len(tracked)
                wins = sum(1 for r in tracked.values() if r.get("won") == "yes")
                losses = sum(1 for r in tracked.values() if r.get("won") == "no")
                pending = total - wins - losses
                print(f"    Tracking: {total} total | {new_count} new | {resolved_count} just resolved | "
                      f"{wins}W / {losses}L / {pending} pending", flush=True)
                if wins + losses > 0:
                    wr = wins / (wins + losses)
                    print(f"    Win rate: {wr:.1%} ({wins}/{wins + losses})", flush=True)

            except Exception as e:
                print(f"    [ERROR] {e}", flush=True)

            print("", flush=True)

            # Wait for next scan
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=args.interval * 60,
                )
                break
            except asyncio.TimeoutError:
                pass

    print(f"\n  Tracker stopped. Results in {TRACKER_CSV}", flush=True)


def handle_signal(sig, frame):
    print(f"\n  Shutting down...", flush=True)
    shutdown_event.set()


def main():
    parser = argparse.ArgumentParser(description="Paper-test consensus trading signal")
    parser.add_argument("--top", type=int, default=25,
                        help="Number of leaderboard wallets to scan (default: 25)")
    parser.add_argument("--min", type=int, default=3,
                        help="Minimum traders for consensus (default: 3)")
    parser.add_argument("--interval", type=int, default=15,
                        help="Minutes between scans (default: 15)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
