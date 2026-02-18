"""
Entry Observer v5 — calibrated fair values + HTF EMA + 1m TEMA exit.

Mimics the live bot's buy-side logic (same thresholds, same Chainlink feed),
using the calibrated fair value table from calibrate.py. Evaluates entries
based on (move_size, elapsed_time) lookups.

After entry, monitors 1-minute TEMA(5)/TEMA(12) for a crossover against the
position direction. If the TEMA crosses against us mid-trade, we "exit" and
score based on BTC direction at exit time. Also logs what the held-to-end
result would have been for comparison.

Outputs:
  data/entry_observations.csv  — one row per price tick after each entry
  Console                      — real-time status + scoreboard

Usage:
  python -u entry_observer.py                        # run with defaults
  python -u entry_observer.py --tick 5               # log BTC every 5 seconds
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
from trend import TrendTracker, HTFEmaTracker
from live_trader import FAIR_VALUE_TABLE, MOVE_BINS, ELAPSED_BINS, lookup_fair_value

# ── Config (mirrors live_trader defaults) ────────────────────
MIN_MOVE_PCT = float(os.getenv("MIN_MOVE_PCT", "0.03"))
ENTRY_START = int(os.getenv("ENTRY_START", "60"))
ENTRY_END = int(os.getenv("ENTRY_END", "840"))

EXIT_MONITOR_START = 600 # seconds into interval before TEMA exit is active (minute 10)
EXIT_CUSHION_PCT = 0.05  # don't TEMA-exit if we're winning by more than this % vs open
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
    def __init__(self, tick_interval=5, max_intervals=None):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.tick_interval = tick_interval
        self.max_intervals = max_intervals
        self.feed = ChainlinkFeed(symbols=["BTC"], on_trade=self._on_trade)
        self.trend = TrendTracker()
        self.htf_ema = HTFEmaTracker()  # EMA(5) on 15m candles
        self.exit_tema = TrendTracker(candle_interval=60, fast_period=5, slow_period=12)
        self.current_interval = None
        self._last_status_bucket = 0
        self._interval_count = 0
        self._entry_count = 0
        self._wins = 0
        self._losses = 0
        self._tema_exits = 0       # TEMA exit signal count
        self._tema_saved = 0       # exits where held result was LOSS (saved us)
        self._tema_cost = 0        # exits where held result was WIN (cost us)
        self._entries = []         # full entry history
        self._first_interval = True  # skip first (partial) interval
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
        base = f"{self._wins}W / {self._losses}L ({pct:.0f}%)"
        if self._tema_exits > 0:
            base += f" | exits: {self._tema_saved}saved/{self._tema_cost}cost"
        return base

    def _get_interval_start(self):
        now = datetime.now(timezone.utc)
        mins = (now.minute // 15) * 15
        base = now.replace(minute=mins, second=0, microsecond=0)
        return int(base.timestamp())

    def _rotate_interval(self):
        ts = self._get_interval_start()
        if self.current_interval is None or self.current_interval.start_ts != ts:
            was_first = self._first_interval
            prev = self.current_interval
            self.current_interval = IntervalState(ts)
            self._last_status_bucket = 0

            if was_first:
                # First interval is partial — we connected mid-interval so open price
                # is wrong.  Don't score it, don't count it.
                self._first_interval = False
                if prev and prev.entry:
                    print(f"\n  >> SKIPPED (partial first interval) | "
                          f"{prev.entry['direction']} — open price unreliable", flush=True)
            else:
                self._interval_count += 1

                if prev and prev.entry:
                    e = prev.entry
                    final_move = prev.move_pct
                    went_up = prev.latest_price >= prev.open_price
                    held_winner = "Up" if went_up else "Down"
                    held_won = e["direction"] == held_winner

                    if e.get("exited"):
                        # TEMA exit — score based on BTC direction at exit time
                        won_direction = e["exit_in_direction"]
                        if held_won:
                            self._tema_cost += 1
                            held_tag = "held->WIN"
                        else:
                            self._tema_saved += 1
                            held_tag = "held->LOSS"
                    else:
                        won_direction = held_won
                        held_tag = None

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

                    if e.get("exited"):
                        print(f"\n  >> RESULT: {result} (TEMA exit) | {e['direction']} | "
                              f"exit ${e['exit_price']:,.2f} | close ${prev.latest_price:,.2f} | "
                              f"{held_tag}", flush=True)
                    else:
                        print(f"\n  >> RESULT: {result} | {e['direction']} ({e.get('move_bucket','?')}%) | "
                              f"fair {e.get('fair_value',0):.3f} | BTC {final_move:+.4f}% | "
                              f"open ${prev.open_price:,.2f} -> close ${prev.latest_price:,.2f}", flush=True)
                    print(f"  >> SCOREBOARD: {self._scoreboard()}", flush=True)

            # Print trend status at interval start
            htf_d = self.htf_ema.get_detail()
            htf_str = f"EMA({self.htf_ema.ema_period})=${htf_d['ema_value']:,.2f} price=${htf_d['price']:,.2f}" if htf_d['ready'] else "warming up..."
            td = self.trend.get_detail()
            tema_str = f"TEMA={td['trend']}" if td['ready'] else ""
            print(f"\n--- {fmt_et_short(ts)}-{fmt_et_short(ts+900)} ET | {self._scoreboard()} | HTF: {htf_str} | {tema_str} ---", flush=True)

            if not was_first and self.max_intervals and self._interval_count > self.max_intervals:
                print(f"\nReached {self.max_intervals} intervals. Stopping.", flush=True)
                self.feed.stop()

    async def _on_trade(self, symbol, price, exchange_ts, local_ts):
        try:
            # Update trackers with every price tick
            self.trend.update_price(price, exchange_ts)
            self.htf_ema.update_price(price, exchange_ts)
            self.exit_tema.update_price(price, exchange_ts)

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

            # If we have an active entry, monitor for TEMA exit and log ticks
            if iv.entry:
                now = time.time()

                # Check for TEMA exit signal (only after minute 10, and if not already exited)
                if not iv.entry.get("exited") and iv.elapsed >= EXIT_MONITOR_START:
                    exit_trend = self.exit_tema.get_trend()
                    entry_dir = iv.entry["direction"]

                    # Detect cross: TEMA state changed AND now opposes position
                    prev_tema = iv.entry.get("prev_exit_tema")
                    skipped_cushion = False
                    if (prev_tema is not None
                            and exit_trend != "Neutral"
                            and exit_trend != prev_tema):
                        is_against = ((entry_dir == "Up" and exit_trend == "Down") or
                                      (entry_dir == "Down" and exit_trend == "Up"))
                        if is_against:
                            secs_since = now - iv.entry["ts"]
                            vs_open = iv.move_pct

                            # How much are we winning by? (positive = in our favor)
                            cushion = abs(vs_open) if (
                                (entry_dir == "Up" and vs_open > 0) or
                                (entry_dir == "Down" and vs_open < 0)
                            ) else 0.0

                            if cushion > EXIT_CUSHION_PCT:
                                # Winning comfortably — keep cross pending so it
                                # fires if our cushion erodes later
                                skipped_cushion = True
                                print(f"  [EXIT SKIP] TEMA {exit_trend} cross but cushion {cushion:.4f}% > {EXIT_CUSHION_PCT}% -- holding (cross stays pending)", flush=True)
                            else:
                                # Flat, marginal, or losing — exit
                                in_dir = ((entry_dir == "Up" and price >= iv.open_price) or
                                          (entry_dir == "Down" and price < iv.open_price))

                                iv.entry["exited"] = True
                                iv.entry["exit_price"] = price
                                iv.entry["exit_ts"] = now
                                iv.entry["exit_in_direction"] = in_dir
                                self._tema_exits += 1

                                ed = self.exit_tema.get_detail()
                                status = "ahead" if in_dir else "behind"
                                print(f"\n  >> TEMA EXIT @ {secs_since:.0f}s | 1m TEMA: {exit_trend} cross | "
                                      f"BTC ${price:,.2f} ({vs_open:+.4f}% vs open) | {status} | "
                                      f"TEMA(5)=${ed['tema_fast']:,.2f} TEMA(12)=${ed['tema_slow']:,.2f}",
                                      flush=True)
                                print(f"  >> (waiting for interval end to compare...)", flush=True)

                    # Track TEMA state — but NOT after a cushion skip, so the
                    # pending cross can re-check on the next tick
                    if exit_trend != "Neutral" and not skipped_cushion:
                        iv.entry["prev_exit_tema"] = exit_trend

                # Log price ticks (skip if already exited)
                if not iv.entry.get("exited"):
                    if now - iv.last_tick_ts >= self.tick_interval:
                        iv.last_tick_ts = now
                        self._write_tick(iv, iv.entry, price)

                        secs_since = now - iv.entry["ts"]
                        vs_entry = ((price - iv.entry["btc_at_entry"]) / iv.entry["btc_at_entry"]) * 100
                        vs_open = iv.move_pct
                        et = self.exit_tema.get_trend()
                        tema_tag = f" | 1m:{et}" if et != "Neutral" else ""
                        print(f"  [{secs_since:5.0f}s] ${price:,.2f} | vs entry {vs_entry:+.4f}% (${price - iv.entry['btc_at_entry']:+.2f}) | vs open {vs_open:+.4f}% (${price - iv.open_price:+.2f}) | {iv.remaining:.0f}s left{tema_tag}", flush=True)
                return  # don't evaluate new entries once we're in a trade

            # Status every 60s
            bucket = int(iv.elapsed) // 60
            if bucket > self._last_status_bucket and iv.elapsed > 10:
                self._last_status_bucket = bucket
                trend = self.trend.get_trend()
                print(f"  {iv.move_pct:+.4f}% | ${price:,.2f} | {iv.remaining:.0f}s left | trend={trend}", flush=True)

            # Signal logic — same as live_trader
            if self._first_interval:
                return  # skip partial first interval — open price is unreliable
            if iv.trade_taken or iv.elapsed < ENTRY_START or iv.elapsed > ENTRY_END:
                return

            abs_move = abs(iv.move_pct)

            if abs_move < MIN_MOVE_PCT:
                if abs_move > 0.02:
                    minute = int(iv.elapsed) // 60
                    if minute != iv.last_log_minute:
                        iv.last_log_minute = minute
                        print(f"  [SIGNAL] {abs_move:.4f}% @ {iv.elapsed:.0f}s — below threshold", flush=True)
                return

            # Look up calibrated fair value from the table
            fair_value, move_bucket, elapsed_bucket = lookup_fair_value(abs_move, iv.elapsed)
            if fair_value is None:
                return

            # Throttle evaluations to once per 30 seconds
            half_min = int(iv.elapsed) // 30
            if half_min == iv.last_eval_half_min:
                return
            iv.last_eval_half_min = half_min

            direction = "Up" if iv.move_pct > 0 else "Down"

            # HTF EMA(5) on 15m — entry filter
            htf_aligned = self.htf_ema.is_aligned(direction)
            if htf_aligned is False:
                minute = int(iv.elapsed) // 60
                if minute != iv.last_log_minute:
                    iv.last_log_minute = minute
                    htf_d = self.htf_ema.get_detail()
                    print(f"  [FILTERED] {direction} {abs_move:.4f}% @ {iv.elapsed:.0f}s — "
                          f"HTF EMA(5)=${htf_d['ema_value']:,.2f} vs ${htf_d['price']:,.2f} misaligned",
                          flush=True)
                return

            # TEMA for diagnostics only (not used for entry decisions)
            trend_dir = self.trend.get_trend()
            td = self.trend.get_detail()

            # Virtual entry — record it
            iv.trade_taken = True
            htf_d = self.htf_ema.get_detail()
            et = self.exit_tema.get_trend()
            iv.entry = {
                "direction": direction,
                "strength": move_bucket,
                "fair_value": fair_value,
                "move_bucket": move_bucket,
                "elapsed_bucket": elapsed_bucket,
                "trend": trend_dir,
                "tema_fast": td.get("tema_fast"),
                "tema_slow": td.get("tema_slow"),
                "htf_ema": htf_d.get("ema_value"),
                "htf_aligned": htf_aligned,
                "btc_at_entry": price,
                "btc_open": iv.open_price,
                "move_at_entry": iv.move_pct,
                "elapsed": iv.elapsed,
                "ts": time.time(),
                "prev_exit_tema": et if et != "Neutral" else None,
                "exited": False,
                "exit_price": None,
                "exit_ts": None,
                "exit_in_direction": None,
            }
            iv.last_tick_ts = time.time()
            self._entry_count += 1

            print(f"\n  >> ENTRY #{self._entry_count}: {direction} | {move_bucket}% @ {elapsed_bucket}s | "
                  f"fair {fair_value:.3f} | ${price:,.2f} | "
                  f"move {iv.move_pct:+.4f}% | {iv.remaining:.0f}s left", flush=True)
            print(f"  >> Price To Beat: ${iv.open_price:,.2f} | Entry BTC: ${price:,.2f} | "
                  f"Cushion: ${price - iv.open_price:+.2f}", flush=True)
            opposite = "Up" if direction == "Down" else "Down"
            ed = self.exit_tema.get_detail()
            if ed['ready']:
                print(f"  >> Exit monitor: 1m TEMA(5)=${ed['tema_fast']:,.2f} / TEMA(12)=${ed['tema_slow']:,.2f} | watching for {opposite} cross", flush=True)
            else:
                print(f"  >> Exit monitor: 1m TEMA(5)/TEMA(12) warming up...", flush=True)
            print(f"  >> Tracking every {self.tick_interval}s until interval ends...", flush=True)

            # Write the first tick immediately
            self._write_tick(iv, iv.entry, price)

        except Exception as e:
            print(f"  [ERR] {e}", flush=True)
            import traceback
            traceback.print_exc()

    async def run(self):
        self._init_csv()

        print(f"Entry Observer v5 — Calibrated fair values + HTF EMA + TEMA exit", flush=True)
        print(f"Min move: {MIN_MOVE_PCT}% | Entry window: {ENTRY_START}s - {ENTRY_END}s", flush=True)
        print(f"HTF filter: EMA({self.htf_ema.ema_period}) on {self.htf_ema.candle_interval // 60}m candles", flush=True)
        print(f"Exit monitor: 1m TEMA(5)/TEMA(12) cross after {EXIT_MONITOR_START}s | cushion skip > {EXIT_CUSHION_PCT}%", flush=True)
        print(f"Post-entry tick interval: {self.tick_interval}s", flush=True)
        print(f"Output: {OUT_FILE}", flush=True)
        if self.max_intervals:
            print(f"Will stop after {self.max_intervals} intervals", flush=True)
        print(f"", flush=True)

        # Bootstrap TEMA with historical candles (diagnostics)
        ok = await self.trend.bootstrap()
        if not ok:
            print("[WARN] TEMA bootstrap failed — trend display will show Neutral until enough candles collected", flush=True)

        # Bootstrap HTF EMA with historical candles (entry filter)
        ok = await self.htf_ema.bootstrap()
        if not ok:
            print("[WARN] HTF EMA bootstrap failed — filter will allow all entries until ready", flush=True)

        # Bootstrap exit TEMA with 1-minute candles
        ok = await self.exit_tema.bootstrap()
        if not ok:
            print("[WARN] Exit TEMA bootstrap failed — exit signals disabled until enough 1m candles", flush=True)

        print(f"\nPrice feed: Chainlink via Polymarket RTDS", flush=True)
        print(f"Ctrl+C to stop\n", flush=True)
        await self.feed.start()

    def summary(self):
        total = self._wins + self._losses
        pct = (self._wins / total * 100) if total > 0 else 0

        print(f"\n{'='*60}")
        print(f"Entry Observer v5 Summary")
        print(f"  Intervals watched: {self._interval_count}")
        print(f"  Virtual entries:   {self._entry_count}")
        print(f"  Results:           {self._wins}W / {self._losses}L ({pct:.0f}%)")
        if self._tema_exits > 0:
            print(f"  TEMA exits:        {self._tema_exits} ({self._tema_saved} saved / {self._tema_cost} cost)")
        print(f"  Data saved to:     {OUT_FILE}")

        if self._entries:
            print(f"\n  {'#':>3}  {'Dir':>5}  {'Move Bucket':>12}  {'Fair':>6}  {'Move@Entry':>11}  {'Final':>10}  {'Result':>8}")
            print(f"  {'-'*3}  {'-'*5}  {'-'*12}  {'-'*6}  {'-'*11}  {'-'*10}  {'-'*8}")
            for i, e in enumerate(self._entries, 1):
                fair = e.get('fair_value', 0)
                result_str = e.get('result', '?')
                if e.get('exited'):
                    result_str += "*"
                print(f"  {i:3d}  {e['direction']:>5}  {e.get('move_bucket','?'):>12}  {fair:>5.1%}  "
                      f"{e['move_at_entry']:+10.4f}%  {e.get('final_move',0):+9.4f}%  {result_str:>8}")
            if self._tema_exits > 0:
                print(f"\n  (* = TEMA exit)")

        print(f"{'='*60}")
        if self._csv_file:
            self._csv_file.close()


if __name__ == "__main__":
    tick = 5
    max_iv = None

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--tick" and i + 1 < len(args):
            tick = int(args[i + 1])
        if arg == "--intervals" and i + 1 < len(args):
            max_iv = int(args[i + 1])

    observer = EntryObserver(tick_interval=tick, max_intervals=max_iv)
    try:
        asyncio.run(observer.run())
    except KeyboardInterrupt:
        print("\nStopping...")
    observer.summary()
