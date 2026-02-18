"""
calibrate.py — Fair value calibration from historical BTC price data.

Fetches 1-minute BTC-USD candles from Coinbase, simulates every 15-minute
interval, and computes empirical win rates bucketed by (move_size, elapsed_time).

The output is a JSON lookup table that replaces the bot's static fair values
(0.70/0.75/0.85) with data-driven probabilities.

Usage:
    python calibrate.py                    # default: 90 days of data
    python calibrate.py --days 180         # custom lookback
    python calibrate.py --refresh          # re-fetch even if cache exists
"""
import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

try:
    import aiohttp
except ImportError:
    print("aiohttp required: pip install aiohttp")
    sys.exit(1)

from trend import calc_tema

# ── Configuration ─────────────────────────────────────────────
DATA_DIR = "data"
CACHE_FILE = os.path.join(DATA_DIR, "coinbase_btc_1m.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "fair_values.json")

COINBASE_BASE = "https://api.exchange.coinbase.com"
PRODUCT = "BTC-USD"
GRANULARITY = 60  # 1-minute candles
MAX_PER_REQUEST = 300

INTERVAL_SECS = 900  # 15 minutes

# TEMA parameters (match live bot defaults)
TEMA_CANDLE_SECS = 300  # 5-minute candles for TEMA
TEMA_FAST = 10
TEMA_SLOW = 80

# Bucket definitions — aligned with the bot's decision points
MOVE_BINS = [
    (0.03, 0.05, "0.03-0.05"),
    (0.05, 0.10, "0.05-0.10"),
    (0.10, 0.20, "0.10-0.20"),
    (0.20, 999.0, "0.20+"),
]

ELAPSED_BINS = [
    (60,  180, "60-180"),
    (180, 420, "180-420"),
    (420, 600, "420-600"),
    (600, 841, "600-840"),
]

MIN_SAMPLES = 30  # minimum count before trusting a cell


# ── Coinbase Data Fetch ───────────────────────────────────────

async def fetch_candles(days=90, refresh=False):
    """Fetch 1-minute BTC-USD candles from Coinbase. Caches locally."""
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(CACHE_FILE) and not refresh:
        with open(CACHE_FILE, "r") as f:
            candles = json.load(f)
        print(f"Loaded {len(candles):,} cached candles from {CACHE_FILE}")

        # Check if cache covers the requested range
        if candles:
            oldest = datetime.fromtimestamp(candles[0]["ts"], tz=timezone.utc)
            newest = datetime.fromtimestamp(candles[-1]["ts"], tz=timezone.utc)
            span = (newest - oldest).days
            print(f"  Range: {oldest:%Y-%m-%d} to {newest:%Y-%m-%d} ({span} days)")
            if span >= days - 2:
                return candles
            print(f"  Cache only covers {span} days, need {days}. Re-fetching...")

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    print(f"Fetching {days} days of 1-min BTC-USD candles from Coinbase...")
    print(f"  Range: {start_dt:%Y-%m-%d %H:%M} to {end_dt:%Y-%m-%d %H:%M} UTC")

    all_candles = []
    cursor = start_dt
    req_count = 0
    t0 = time.time()

    async with aiohttp.ClientSession() as session:
        while cursor < end_dt:
            chunk_end = min(cursor + timedelta(minutes=MAX_PER_REQUEST), end_dt)

            params = {
                "start": int(cursor.timestamp()),
                "end": int(chunk_end.timestamp()),
                "granularity": GRANULARITY,
            }

            url = f"{COINBASE_BASE}/products/{PRODUCT}/candles"

            try:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        print("  Rate limited — waiting 3s...", flush=True)
                        await asyncio.sleep(3)
                        continue

                    if resp.status != 200:
                        body = await resp.text()
                        print(f"  HTTP {resp.status}: {body[:200]}", flush=True)
                        await asyncio.sleep(1)
                        continue

                    data = await resp.json()
                    for row in data:
                        all_candles.append({
                            "ts": row[0],
                            "low": float(row[1]),
                            "high": float(row[2]),
                            "open": float(row[3]),
                            "close": float(row[4]),
                            "volume": float(row[5]),
                        })

            except asyncio.TimeoutError:
                print("  Timeout — retrying in 2s...", flush=True)
                await asyncio.sleep(2)
                continue
            except Exception as e:
                print(f"  Error: {e} — retrying in 2s...", flush=True)
                await asyncio.sleep(2)
                continue

            req_count += 1
            cursor = chunk_end

            if req_count % 100 == 0:
                pct = (cursor - start_dt) / (end_dt - start_dt) * 100
                elapsed = time.time() - t0
                print(f"  {req_count} requests | {len(all_candles):,} candles | "
                      f"{pct:.0f}% | {elapsed:.0f}s", flush=True)

            await asyncio.sleep(0.2)

    # Sort ascending by timestamp, deduplicate
    all_candles.sort(key=lambda c: c["ts"])
    seen = set()
    deduped = []
    for c in all_candles:
        if c["ts"] not in seen:
            seen.add(c["ts"])
            deduped.append(c)
    all_candles = deduped

    elapsed = time.time() - t0
    print(f"  Done: {len(all_candles):,} candles in {req_count} requests ({elapsed:.0f}s)")

    with open(CACHE_FILE, "w") as f:
        json.dump(all_candles, f)
    print(f"  Cached to {CACHE_FILE}")

    return all_candles


# ── TEMA Computation ──────────────────────────────────────────

def build_5min_candles(candles_1m):
    """Aggregate 1-minute candles into 5-minute candles for TEMA."""
    by_ts = {}
    for c in candles_1m:
        bucket = (c["ts"] // TEMA_CANDLE_SECS) * TEMA_CANDLE_SECS
        if bucket not in by_ts:
            by_ts[bucket] = {
                "ts": bucket,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
            }
        else:
            entry = by_ts[bucket]
            entry["high"] = max(entry["high"], c["high"])
            entry["low"] = min(entry["low"], c["low"])
            entry["close"] = c["close"]

    result = sorted(by_ts.values(), key=lambda c: c["ts"])
    return result


def compute_tema_series(candles_5m):
    """Compute TEMA(fast) and TEMA(slow) at every 5-min candle close.

    Returns a dict: timestamp -> {"tema_fast": float, "tema_slow": float, "trend": str}
    """
    closes = [c["close"] for c in candles_5m]
    timestamps = [c["ts"] for c in candles_5m]

    series = {}
    min_needed = TEMA_SLOW * 3 + 1

    for i in range(min_needed, len(closes)):
        window = closes[:i + 1]
        tf = calc_tema(window, TEMA_FAST)
        ts = calc_tema(window, TEMA_SLOW)

        if tf is not None and ts is not None:
            trend = "Up" if tf > ts else ("Down" if tf < ts else "Neutral")
            series[timestamps[i]] = {
                "tema_fast": tf,
                "tema_slow": ts,
                "trend": trend,
            }

    return series


def get_tema_at(tema_series, ts):
    """Look up TEMA state at a given timestamp.

    Finds the most recent 5-min candle close before the given time.
    """
    candle_ts = (ts // TEMA_CANDLE_SECS) * TEMA_CANDLE_SECS

    # Try current and previous candle (current may not be closed yet)
    for offset in [0, -TEMA_CANDLE_SECS, -2 * TEMA_CANDLE_SECS]:
        key = candle_ts + offset
        if key in tema_series:
            return tema_series[key]

    return None


# ── Interval Simulation ──────────────────────────────────────

def simulate_intervals(candles_1m, tema_series):
    """Replay all 15-minute intervals and record observations.

    For each interval, at each minute mark (60s through 840s), records the
    current move from open, direction, and whether it matched the final outcome.
    Also tags each observation with TEMA alignment.
    """
    by_ts = {c["ts"]: c for c in candles_1m}

    min_ts = candles_1m[0]["ts"]
    max_ts = candles_1m[-1]["ts"]

    # Align to first complete 15-minute boundary
    interval_start = min_ts - (min_ts % INTERVAL_SECS) + INTERVAL_SECS

    observations = []
    stats = {"total": 0, "complete": 0, "skipped": 0}

    while interval_start + INTERVAL_SECS <= max_ts:
        stats["total"] += 1

        # Need all 15 one-minute candles (0-14)
        interval_candles = []
        for i in range(15):
            ts = interval_start + (i * 60)
            if ts in by_ts:
                interval_candles.append(by_ts[ts])
            else:
                break

        if len(interval_candles) < 15:
            stats["skipped"] += 1
            interval_start += INTERVAL_SECS
            continue

        stats["complete"] += 1

        open_price = interval_candles[0]["open"]
        close_price = interval_candles[14]["close"]
        final_winner = "Up" if close_price >= open_price else "Down"

        # TEMA state at the start of this interval
        tema = get_tema_at(tema_series, interval_start)

        # Observe at each minute from 60s to 840s
        for minute_idx in range(14):
            candle = interval_candles[minute_idx]
            price = candle["close"]
            elapsed = (minute_idx + 1) * 60

            move_pct = ((price - open_price) / open_price) * 100
            abs_move = abs(move_pct)

            if abs_move < 0.01:
                continue

            direction = "Up" if move_pct > 0 else "Down"
            won = direction == final_winner

            tema_trend = tema["trend"] if tema else "Neutral"
            tema_aligned = (direction == tema_trend) if tema_trend != "Neutral" else None

            observations.append({
                "interval_ts": interval_start,
                "elapsed": elapsed,
                "move_pct": move_pct,
                "abs_move": abs_move,
                "direction": direction,
                "final_winner": final_winner,
                "won": won,
                "tema_trend": tema_trend,
                "tema_aligned": tema_aligned,
            })

        interval_start += INTERVAL_SECS

    print(f"\nInterval simulation:")
    print(f"  Total 15-min slots:   {stats['total']:,}")
    print(f"  Complete (all data):  {stats['complete']:,}")
    print(f"  Skipped (gaps):       {stats['skipped']:,}")
    print(f"  Observations:         {len(observations):,}")

    return observations, stats


# ── Bucketing & Analysis ─────────────────────────────────────

def classify_move(abs_pct):
    for lo, hi, label in MOVE_BINS:
        if lo <= abs_pct < hi:
            return label
    return None


def classify_elapsed(secs):
    for lo, hi, label in ELAPSED_BINS:
        if lo <= secs < hi:
            return label
    return None


def compute_table(observations, filter_fn=None):
    """Compute a win-rate table from observations, optionally filtered."""
    buckets = defaultdict(lambda: {"wins": 0, "total": 0})

    for obs in observations:
        if filter_fn and not filter_fn(obs):
            continue

        mb = classify_move(obs["abs_move"])
        eb = classify_elapsed(obs["elapsed"])
        if mb is None or eb is None:
            continue

        key = (mb, eb)
        buckets[key]["total"] += 1
        if obs["won"]:
            buckets[key]["wins"] += 1

    table = {}
    for (mb, eb), s in buckets.items():
        if mb not in table:
            table[mb] = {}
        wr = s["wins"] / s["total"] if s["total"] > 0 else None
        table[mb][eb] = {"win_rate": round(wr, 4) if wr else None, "count": s["total"]}

    return table


def print_table(table, title, move_order, elapsed_order):
    """Print a formatted win-rate table."""
    col_w = 16
    header = f"  {'Move':>12}"
    for eb in elapsed_order:
        header += f"  {eb + 's':>{col_w}}"
    print(f"\n  {title}")
    print(f"  {'=' * (14 + (col_w + 2) * len(elapsed_order))}")
    print(header)
    print(f"  {'-' * (14 + (col_w + 2) * len(elapsed_order))}")

    for mb in move_order:
        row = f"  {mb + '%':>12}"
        for eb in elapsed_order:
            cell = table.get(mb, {}).get(eb, {})
            wr = cell.get("win_rate")
            n = cell.get("count", 0)
            if wr is not None and n >= MIN_SAMPLES:
                row += f"  {wr:>6.1%} ({n:>4})"
            elif wr is not None:
                row += f"  {wr:>6.1%}*({n:>3})"
            else:
                row += f"  {'—':>{col_w}}"
        print(row)

    print(f"\n  * = fewer than {MIN_SAMPLES} samples (low confidence)")


def print_comparison(table):
    """Compare calibrated values against the bot's current static fair values."""
    print(f"\n  {'=' * 72}")
    print(f"  COMPARISON: Calibrated vs Current Static Values")
    print(f"  {'=' * 72}")
    print(f"  {'Setup':<42} {'Calibrated':>12} {'Current':>10} {'Delta':>8}")
    print(f"  {'-' * 72}")

    comparisons = [
        ("MODERATE (0.05-0.10%, 420-600s)", "0.05-0.10", "420-600", 0.70),
        ("MODERATE (0.05-0.10%, 600-840s)", "0.05-0.10", "600-840", 0.70),
        ("STRONG early (0.10-0.20%, 60-180s)", "0.10-0.20", "60-180", 0.75),
        ("STRONG early (0.10-0.20%, 180-420s)", "0.10-0.20", "180-420", 0.75),
        ("STRONG mid (0.10-0.20%, 420-600s)", "0.10-0.20", "420-600", 0.85),
        ("STRONG late (0.10-0.20%, 600-840s)", "0.10-0.20", "600-840", 0.85),
        ("V.STRONG early (0.20%+, 60-180s)", "0.20+", "60-180", 0.75),
        ("V.STRONG early (0.20%+, 180-420s)", "0.20+", "180-420", 0.75),
        ("V.STRONG mid (0.20%+, 420-600s)", "0.20+", "420-600", 0.85),
        ("V.STRONG late (0.20%+, 600-840s)", "0.20+", "600-840", 0.85),
    ]

    for label, mb, eb, current in comparisons:
        cell = table.get(mb, {}).get(eb, {})
        wr = cell.get("win_rate")
        n = cell.get("count", 0)
        if wr is not None and n >= MIN_SAMPLES:
            delta = wr - current
            flag = " <<<" if abs(delta) >= 0.05 else ""
            print(f"  {label:<42} {wr:>9.1%} n={n:<4} {current:>8.0%}   {delta:>+7.1%}{flag}")
        elif wr is not None:
            print(f"  {label:<42} {wr:>9.1%} n={n:<4} {current:>8.0%}   (low n)")
        else:
            print(f"  {label:<42} {'—':>12} {current:>8.0%}")


# ── Main ─────────────────────────────────────────────────────

async def main():
    days = 90
    refresh = False

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
        if arg == "--refresh":
            refresh = True

    print(f"{'=' * 60}")
    print(f"  Fair Value Calibration")
    print(f"  Source: Coinbase BTC-USD | Lookback: {days} days")
    print(f"{'=' * 60}\n")

    # ── Step 1: Fetch candles ─────────────────────────────────
    candles = await fetch_candles(days=days, refresh=refresh)

    if len(candles) < 1000:
        print(f"\nERROR: Only {len(candles)} candles — not enough data.")
        return

    oldest = datetime.fromtimestamp(candles[0]["ts"], tz=timezone.utc)
    newest = datetime.fromtimestamp(candles[-1]["ts"], tz=timezone.utc)
    print(f"\nData range: {oldest:%Y-%m-%d %H:%M} to {newest:%Y-%m-%d %H:%M} UTC")
    print(f"Total candles: {len(candles):,}")

    # ── Step 2: Build TEMA series ─────────────────────────────
    print(f"\nComputing TEMA({TEMA_FAST}/{TEMA_SLOW}) on 5-min candles...")
    candles_5m = build_5min_candles(candles)
    tema_series = compute_tema_series(candles_5m)
    print(f"  5-min candles: {len(candles_5m):,}")
    print(f"  TEMA data points: {len(tema_series):,}")

    # ── Step 3: Simulate intervals ────────────────────────────
    observations, sim_stats = simulate_intervals(candles, tema_series)

    if not observations:
        print("\nERROR: No valid observations. Check data quality.")
        return

    # ── Step 4: Compute tables ────────────────────────────────
    move_order = [b[2] for b in MOVE_BINS]
    elapsed_order = [b[2] for b in ELAPSED_BINS]

    # 4a. Overall win rates (no TEMA filter)
    all_table = compute_table(observations)
    print_table(all_table, "ALL OBSERVATIONS (no TEMA filter)", move_order, elapsed_order)

    # 4b. TEMA-aligned only (direction matches TEMA trend)
    aligned_table = compute_table(observations, lambda o: o["tema_aligned"] is True)
    print_table(aligned_table, "TEMA-ALIGNED ONLY (direction matches trend)", move_order, elapsed_order)

    # 4c. TEMA-misaligned (direction opposes TEMA trend)
    misaligned_table = compute_table(observations, lambda o: o["tema_aligned"] is False)
    print_table(misaligned_table, "TEMA-MISALIGNED (direction opposes trend — bot filters these)", move_order, elapsed_order)

    # ── Step 5: TEMA filter impact ────────────────────────────
    print(f"\n  {'=' * 60}")
    print(f"  TEMA FILTER IMPACT")
    print(f"  {'=' * 60}")

    for mb in move_order:
        for eb in elapsed_order:
            a = aligned_table.get(mb, {}).get(eb, {})
            m = misaligned_table.get(mb, {}).get(eb, {})
            a_wr = a.get("win_rate")
            m_wr = m.get("win_rate")
            a_n = a.get("count", 0)
            m_n = m.get("count", 0)

            if a_wr and m_wr and a_n >= MIN_SAMPLES and m_n >= MIN_SAMPLES:
                lift = a_wr - m_wr
                print(f"  {mb}% @ {eb}s: aligned {a_wr:.1%} vs opposed {m_wr:.1%} "
                      f"(+{lift:.1%} lift, n={a_n}/{m_n})")

    # ── Step 6: Comparison with current bot values ────────────
    print_comparison(aligned_table)

    # ── Step 7: Save output ───────────────────────────────────
    # The bot should use the TEMA-aligned table since it already filters
    bot_table = {}
    for mb in move_order:
        bot_table[mb] = {}
        for eb in elapsed_order:
            cell = aligned_table.get(mb, {}).get(eb, {})
            wr = cell.get("win_rate")
            n = cell.get("count", 0)
            bot_table[mb][eb] = wr if (wr is not None and n >= MIN_SAMPLES) else None

    output = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "data_source": "coinbase",
        "product": PRODUCT,
        "lookback_days": days,
        "candle_count": len(candles),
        "interval_count": sim_stats["complete"],
        "observation_count": len(observations),
        "tema_params": {"fast": TEMA_FAST, "slow": TEMA_SLOW, "candle_secs": TEMA_CANDLE_SECS},
        "move_bins": [b[2] for b in MOVE_BINS],
        "elapsed_bins": [b[2] for b in ELAPSED_BINS],
        "all_observations": all_table,
        "tema_aligned": aligned_table,
        "tema_misaligned": misaligned_table,
        "bot_table": bot_table,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    valid_cells = sum(1 for mb in bot_table.values() for v in mb.values() if v is not None)
    total_cells = len(move_order) * len(elapsed_order)
    print(f"\n  Saved to {OUTPUT_FILE}")
    print(f"  Bot table: {valid_cells}/{total_cells} cells have sufficient data")

    # ── Step 8: Quick summary ─────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  KEY TAKEAWAYS")
    print(f"{'=' * 60}")

    # Overall momentum persistence rate
    total_obs = len(observations)
    total_wins = sum(1 for o in observations if o["won"])
    overall_wr = total_wins / total_obs if total_obs > 0 else 0
    print(f"  Baseline momentum persistence: {overall_wr:.1%} "
          f"(across all {total_obs:,} observations)")

    aligned_obs = [o for o in observations if o["tema_aligned"] is True]
    if aligned_obs:
        aligned_wins = sum(1 for o in aligned_obs if o["won"])
        aligned_wr = aligned_wins / len(aligned_obs)
        print(f"  TEMA-aligned persistence:      {aligned_wr:.1%} "
              f"(across {len(aligned_obs):,} observations)")

    misaligned_obs = [o for o in observations if o["tema_aligned"] is False]
    if misaligned_obs:
        misaligned_wins = sum(1 for o in misaligned_obs if o["won"])
        misaligned_wr = misaligned_wins / len(misaligned_obs)
        print(f"  TEMA-opposed persistence:      {misaligned_wr:.1%} "
              f"(across {len(misaligned_obs):,} observations)")

    print()


if __name__ == "__main__":
    asyncio.run(main())
