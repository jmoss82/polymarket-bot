"""
Entry Observer — paper-mode signal tracker with TEMA trend filter.

Mimics the live bot's buy-side logic (same thresholds, same Chainlink feed),
plus a TEMA(10)/TEMA(80) trend filter on 5-min candles. Only takes entries
that align with the trend direction.

Tracks results and prints a running W/L scoreboard.

Outputs:
  data/entry_observations.csv  — one row per price tick after each entry
  Console                      — real-time status + scoreboard

Usage:
  python -u entry_observer.py                        # run with defaults
  python -u entry_observer.py --tick 5               # log BTC every 5 seconds
  python -u entry_observer.py --no-filter            # disable TEMA filter (baseline comparison)
  python -u entry_observer.py --intervals 20         # stop after 20 intervals

Ctrl+C to stop and print summary.
"""
import asyncio
import csv
import os
import sys
import time
from datetime import datetime, timezone, timedelta

from chainlink_ws import ChainlinkFeed
from trend import TrendTracker

# ── Config (mirrors live_trader defaults) ────────────────────
MIN_MOVE_PCT = float(os.getenv("MIN_MOVE_PCT", "0.05"))
STRONG_MOVE_PCT = float(os.getenv("STRONG_MOVE_PCT", "0.10"))
ENTRY_START = int(os.getenv("ENTRY_START", "45"))
ENTRY_END = int(os.getenv("ENTRY_END", "840"))

TICK_INTERVAL = 5        # seconds between post-entry price logs
DATA_DIR = "data"
OUT_FILE = os.path.join(DATA_DIR, "entry_observations.csv")

ET = timezone(timedelta(hours=-5))


def fmt_et(ts):
    return datetime.fromtimestamp(ts, tz=ET).strftime("%I:%M:%S %p")


def fmt_et_short(ts):
    return datetime.fromtimestamp(ts, tz=ET).strftime("%I:%M %p")


class IntervalState:
    def __init__(self, start_ts):
        self.start_ts = start_ts
        self.end_ts = start_ts + 900
        self.open_price = None
        self.latest_price = None
        self.high_price = None
        self.low_price = None
        self.trade_taken = False
        self.entry = None          # dict with entry details
        self.last_log_minute = -1
        self.last_eval_half_min = -1
        self.last_tick_ts = 0      # throttle post-entry logging

    @property
    def elapsed(self):
        return time.time() - self.start_ts

    @property
    def remaining(self):
        return self.end_ts - time.time()

    @property
    def move_pct(self):
        if self.open_price and self.latest_price:
            return ((self.latest_price - self.open_price) / self.open_price) * 100
        return 0.0


class EntryObserver:
    def __init__(self, tick_interval=5, max_intervals=None, use_trend_filter=True):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.tick_interval = tick_interval
        self.max_intervals = max_intervals
        self.use_trend_filter = use_trend_filter
        self.feed = ChainlinkFeed(symbols=["BTC"], on_trade=self._on_trade)
        self.trend = TrendTracker()
        self.current_interval = None
        self._last_status_bucket = 0
        self._interval_count = 0
        self._entry_count = 0
        self._wins = 0
        self._losses = 0
        self._filtered_out = 0    # signals blocked by TEMA filter
        self._entries = []         # full entry history
        self._csv_writer = None
        self._csv_file = None

    def _init_csv(self):
        file_exists = os.path.exists(OUT_FILE) and os.path.getsize(OUT_FILE) > 0
        self._csv_file = open(OUT_FILE, "a", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        if not file_exists:
            self._csv_writer.writerow([
                "interval_start_utc",
                "interval_start_et",
                "signal_direction",
                "signal_strength",
                "trend_direction",
                "tema_fast",
                "tema_slow",
                "btc_open",
                "btc_at_entry",
                "move_at_entry_pct",
                "entry_elapsed_s",
                "tick_time_utc",
                "tick_elapsed_s",
                "secs_since_entry",
                "secs_remaining",
                "btc_price",
                "btc_vs_open_pct",
                "btc_vs_entry_pct",
                "btc_vs_open_dollars",
                "btc_vs_entry_dollars",
            ])
            self._csv_file.flush()

    def _write_tick(self, iv, entry, btc_price):
        now = time.time()
        secs_since_entry = now - entry["ts"]
        move_vs_open = ((btc_price - iv.open_price) / iv.open_price) * 100
        move_vs_entry = ((btc_price - entry["btc_at_entry"]) / entry["btc_at_entry"]) * 100
        dollars_vs_open = btc_price - iv.open_price
        dollars_vs_entry = btc_price - entry["btc_at_entry"]

        self._csv_writer.writerow([
            datetime.fromtimestamp(iv.start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            fmt_et_short(iv.start_ts),
            entry["direction"],
            entry["strength"],
            entry.get("trend", "?"),
            entry.get("tema_fast", ""),
            entry.get("tema_slow", ""),
            f"{iv.open_price:.2f}",
            f"{entry['btc_at_entry']:.2f}",
            f"{entry['move_at_entry']:.4f}",
            f"{entry['elapsed']:.0f}",
            datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            f"{iv.elapsed:.0f}",
            f"{secs_since_entry:.0f}",
            f"{iv.remaining:.0f}",
            f"{btc_price:.2f}",
            f"{move_vs_open:.4f}",
            f"{move_vs_entry:.4f}",
            f"{dollars_vs_open:.2f}",
            f"{dollars_vs_entry:.2f}",
        ])
        self._csv_file.flush()

    def _scoreboard(self):
        total = self._wins + self._losses
        if total == 0:
            return "0W / 0L"
        pct = (self._wins / total) * 100
        return f"{self._wins}W / {self._losses}L ({pct:.0f}%)"

    def _get_interval_start(self):
        now = datetime.now(timezone.utc)
        mins = (now.minute // 15) * 15
        base = now.replace(minute=mins, second=0, microsecond=0)
        return int(base.timestamp())

    def _rotate_interval(self):
        ts = self._get_interval_start()
        if self.current_interval is None or self.current_interval.start_ts != ts:
            prev = self.current_interval
            self.current_interval = IntervalState(ts)
            self._last_status_bucket = 0
            self._interval_count += 1

            if prev and prev.entry:
                # Resolve previous interval's entry
                e = prev.entry
                final_move = prev.move_pct
                final_vs_entry = ((prev.latest_price - e["btc_at_entry"]) / e["btc_at_entry"]) * 100
                won_direction = (final_move > 0 and e["direction"] == "Up") or \
                                (final_move < 0 and e["direction"] == "Down")

                if won_direction:
                    self._wins += 1
                    result = "WIN"
                else:
                    self._losses += 1
                    result = "LOSS"

                e["result"] = result
                e["final_move"] = final_move
                e["btc_close"] = prev.latest_price
                self._entries.append(e)

                print(f"\n  >> RESULT: {result} | {e['direction']} (trend: {e.get('trend','?')}) | "
                      f"BTC {final_move:+.4f}% | "
                      f"open ${prev.open_price:,.2f} -> close ${prev.latest_price:,.2f}", flush=True)
                print(f"  >> SCOREBOARD: {self._scoreboard()} | "
                      f"Filtered out: {self._filtered_out}", flush=True)

            # Print trend status at interval start
            td = self.trend.get_detail()
            trend_str = f"TEMA({self.trend.fast_period})=${td['tema_fast']:,.2f} / TEMA({self.trend.slow_period})=${td['tema_slow']:,.2f} -> {td['trend']}" if td['ready'] else "warming up..."
            print(f"\n--- {fmt_et_short(ts)}-{fmt_et_short(ts+900)} ET | {self._scoreboard()} | Trend: {trend_str} ---", flush=True)

            if self.max_intervals and self._interval_count > self.max_intervals:
                print(f"\nReached {self.max_intervals} intervals. Stopping.", flush=True)
                self.feed.stop()

    async def _on_trade(self, symbol, price, exchange_ts, local_ts):
        try:
            # Update trend tracker with every price tick
            self.trend.update_price(price, exchange_ts)

            self._rotate_interval()
            iv = self.current_interval

            if iv.open_price is None:
                iv.open_price = price
                iv.high_price = price
                iv.low_price = price
                print(f"  Open: ${price:,.2f}", flush=True)

            iv.latest_price = price
            if price > (iv.high_price or 0):
                iv.high_price = price
            if price < (iv.low_price or float('inf')):
                iv.low_price = price

            # If we have an active entry, log BTC price ticks
            if iv.entry:
                now = time.time()
                if now - iv.last_tick_ts >= self.tick_interval:
                    iv.last_tick_ts = now
                    self._write_tick(iv, iv.entry, price)

                    secs_since = now - iv.entry["ts"]
                    vs_entry = ((price - iv.entry["btc_at_entry"]) / iv.entry["btc_at_entry"]) * 100
                    vs_open = iv.move_pct
                    print(f"  [{secs_since:5.0f}s] ${price:,.2f} | vs entry {vs_entry:+.4f}% (${price - iv.entry['btc_at_entry']:+.2f}) | vs open {vs_open:+.4f}% (${price - iv.open_price:+.2f}) | {iv.remaining:.0f}s left", flush=True)
                return  # don't evaluate new entries once we're in a trade

            # Status every 60s
            bucket = int(iv.elapsed) // 60
            if bucket > self._last_status_bucket and iv.elapsed > 10:
                self._last_status_bucket = bucket
                trend = self.trend.get_trend()
                print(f"  {iv.move_pct:+.4f}% | ${price:,.2f} | {iv.remaining:.0f}s left | trend={trend}", flush=True)

            # Signal logic — same as live_trader
            if iv.trade_taken or iv.elapsed < ENTRY_START or iv.elapsed > ENTRY_END:
                return

            abs_move = abs(iv.move_pct)
            if abs_move >= STRONG_MOVE_PCT:
                strength = "STRONG"
            elif abs_move >= MIN_MOVE_PCT and iv.elapsed > 420:
                strength = "MODERATE"
            else:
                if abs_move > 0.03:
                    minute = int(iv.elapsed) // 60
                    if minute != iv.last_log_minute:
                        iv.last_log_minute = minute
                        print(f"  [SIGNAL] {abs_move:.4f}% @ {iv.elapsed:.0f}s — below threshold", flush=True)
                return

            # Throttle evaluations to once per 30 seconds
            half_min = int(iv.elapsed) // 30
            if half_min == iv.last_eval_half_min:
                return
            iv.last_eval_half_min = half_min

            direction = "Up" if iv.move_pct > 0 else "Down"

            # ── TEMA Trend Filter ──────────────────────────────────
            trend_dir = self.trend.get_trend()
            td = self.trend.get_detail()

            if self.use_trend_filter and trend_dir != "Neutral":
                if direction != trend_dir:
                    self._filtered_out += 1
                    print(f"  [FILTERED] {strength} {direction} blocked — trend is {trend_dir} "
                          f"(TEMA {td['tema_fast']:,.0f}/{td['tema_slow']:,.0f}, gap {td['gap_pct']:+.4f}%)", flush=True)
                    return

            # Virtual entry — record it
            iv.trade_taken = True
            iv.entry = {
                "direction": direction,
                "strength": strength,
                "trend": trend_dir,
                "tema_fast": td.get("tema_fast"),
                "tema_slow": td.get("tema_slow"),
                "btc_at_entry": price,
                "btc_open": iv.open_price,
                "move_at_entry": iv.move_pct,
                "elapsed": iv.elapsed,
                "ts": time.time(),
            }
            iv.last_tick_ts = time.time()
            self._entry_count += 1

            trend_tag = f" | trend={trend_dir}" if trend_dir != "Neutral" else " | trend=N/A"
            print(f"\n  >> ENTRY #{self._entry_count}: {strength} {direction} @ ${price:,.2f} | "
                  f"move {iv.move_pct:+.4f}% | open ${iv.open_price:,.2f} | "
                  f"{iv.elapsed:.0f}s elapsed | {iv.remaining:.0f}s left{trend_tag}", flush=True)
            print(f"  >> Price To Beat: ${iv.open_price:,.2f} | Entry BTC: ${price:,.2f} | "
                  f"Cushion: ${price - iv.open_price:+.2f}", flush=True)
            if td['ready']:
                print(f"  >> TEMA({self.trend.fast_period})=${td['tema_fast']:,.2f} | "
                      f"TEMA({self.trend.slow_period})=${td['tema_slow']:,.2f} | "
                      f"gap {td['gap_pct']:+.4f}%", flush=True)
            print(f"  >> Tracking every {self.tick_interval}s until interval ends...", flush=True)

            # Write the first tick immediately
            self._write_tick(iv, iv.entry, price)

        except Exception as e:
            print(f"  [ERR] {e}", flush=True)
            import traceback
            traceback.print_exc()

    async def run(self):
        self._init_csv()

        filter_mode = "ON" if self.use_trend_filter else "OFF (baseline)"
        print(f"Entry Observer v2 — TEMA trend filter {filter_mode}", flush=True)
        print(f"Signal thresholds: STRONG >= {STRONG_MOVE_PCT}% | MODERATE >= {MIN_MOVE_PCT}% (after 420s)", flush=True)
        print(f"Entry window: {ENTRY_START}s - {ENTRY_END}s", flush=True)
        print(f"TEMA: fast={self.trend.fast_period} / slow={self.trend.slow_period} on {self.trend.candle_interval // 60}min candles", flush=True)
        print(f"Post-entry tick interval: {self.tick_interval}s", flush=True)
        print(f"Output: {OUT_FILE}", flush=True)
        if self.max_intervals:
            print(f"Will stop after {self.max_intervals} intervals", flush=True)
        print(f"", flush=True)

        # Bootstrap TEMA with historical candles
        ok = await self.trend.bootstrap()
        if not ok:
            print("[WARN] TEMA bootstrap failed — trend filter will show Neutral until enough candles collected", flush=True)

        print(f"\nPrice feed: Chainlink via Polymarket RTDS", flush=True)
        print(f"Ctrl+C to stop\n", flush=True)
        await self.feed.start()

    def summary(self):
        total = self._wins + self._losses
        pct = (self._wins / total * 100) if total > 0 else 0

        print(f"\n{'='*60}")
        print(f"Entry Observer Summary")
        print(f"  Intervals watched: {self._interval_count}")
        print(f"  Virtual entries:   {self._entry_count}")
        print(f"  Results:           {self._wins}W / {self._losses}L ({pct:.0f}%)")
        print(f"  Filtered out:      {self._filtered_out} (signals blocked by TEMA)")
        print(f"  TEMA filter:       {'ON' if self.use_trend_filter else 'OFF'}")
        print(f"  Data saved to:     {OUT_FILE}")

        if self._entries:
            print(f"\n  {'#':>3}  {'Dir':>5}  {'Trend':>6}  {'Strength':>8}  {'Move@Entry':>11}  {'Final':>10}  {'Result':>6}")
            print(f"  {'-'*3}  {'-'*5}  {'-'*6}  {'-'*8}  {'-'*11}  {'-'*10}  {'-'*6}")
            for i, e in enumerate(self._entries, 1):
                print(f"  {i:3d}  {e['direction']:>5}  {e.get('trend','?'):>6}  {e['strength']:>8}  "
                      f"{e['move_at_entry']:+10.4f}%  {e.get('final_move',0):+9.4f}%  {e.get('result','?'):>6}")

        print(f"{'='*60}")
        if self._csv_file:
            self._csv_file.close()


if __name__ == "__main__":
    tick = 5
    max_iv = None
    use_filter = True

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--tick" and i + 1 < len(args):
            tick = int(args[i + 1])
        if arg == "--intervals" and i + 1 < len(args):
            max_iv = int(args[i + 1])
        if arg == "--no-filter":
            use_filter = False

    observer = EntryObserver(tick_interval=tick, max_intervals=max_iv, use_trend_filter=use_filter)
    try:
        asyncio.run(observer.run())
    except KeyboardInterrupt:
        print("\nStopping...")
    observer.summary()
