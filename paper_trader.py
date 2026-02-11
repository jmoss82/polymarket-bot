"""
Paper trader for BTC 15-min Up/Down markets.
Fully standalone — no AI model needed. Run it and forget it.

Strategy: "Momentum Sniper"
- Monitor Binance BTC price during each 15-min interval
- Track price delta vs interval open
- When price move is decisive AND Polymarket odds are lagging, take a position
- Hold to resolution (binary market)

All trades logged to data/paper_trades.json
Console output kept minimal — just signals and results.

Usage: python -u paper_trader.py
Stop:  Ctrl+C (prints session summary)
"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from binance_ws import BinanceFeed
import aiohttp

# ── Config ──────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
BANKROLL_START = 123.0
BET_SIZE = 5.0
MIN_MOVE_PCT = 0.10
STRONG_MOVE_PCT = 0.15
ENTRY_WINDOW = (300, 840)  # enter between 5:00 and 14:00 into interval

DATA_DIR = "data"
TRADES_FILE = os.path.join(DATA_DIR, "paper_trades.json")
LOG_FILE = os.path.join(DATA_DIR, "paper_log.jsonl")
TRADES_CSV = os.path.join(DATA_DIR, "paper_trades.csv")
TRADES_TXT = os.path.join(DATA_DIR, "paper_log.txt")

ET = timezone(timedelta(hours=-5))


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_state():
    """Load existing trades and bankroll from disk."""
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            state = json.load(f)
            return state.get("trades", []), state.get("bankroll", BANKROLL_START)
    return [], BANKROLL_START


def save_state(trades, bankroll):
    with open(TRADES_FILE, "w") as f:
        json.dump({"trades": trades, "bankroll": bankroll, "updated": time.time()}, f, indent=2)
    # Write CSV
    _write_trades_csv(trades, bankroll)


def _write_trades_csv(trades, bankroll):
    """Rewrite the full CSV from trade list."""
    import csv
    headers = ["#", "Time (ET)", "Interval", "Side", "Strength", "Entry Price",
               "Move at Entry", "BTC Open", "BTC Close", "Final Move",
               "Result", "P&L", "Bankroll"]
    with open(TRADES_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        running = BANKROLL_START
        for i, t in enumerate(trades, 1):
            running += t.get("pnl", 0)
            w.writerow([
                i,
                fmt_et(t.get("ts", 0)),
                t.get("slug", ""),
                t.get("side", ""),
                t.get("strength", ""),
                f"{t.get('entry_price', 0):.3f}",
                f"{t.get('move_at_entry', 0):+.3f}%",
                f"${t.get('btc_open', 0):,.2f}",
                f"${t.get('btc_close', 0):,.2f}",
                f"{t.get('final_move', 0):+.3f}%",
                "WIN" if t.get("won") else "LOSS",
                f"${t.get('pnl', 0):+.2f}",
                f"${running:.2f}",
            ])
        w.writerow([])
        w.writerow(["", "", "", "", "", "", "", "", "", "",
                     f"{sum(1 for t in trades if t.get('won'))}/{len(trades)}",
                     f"${sum(t.get('pnl',0) for t in trades):+.2f}",
                     f"${bankroll:.2f}"])


def log_event(event_type, data):
    """Append event to JSONL log and human-readable txt."""
    entry = {"ts": time.time(), "type": event_type, **data}
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    # Human-readable txt
    ts_str = fmt_et(time.time())
    with open(TRADES_TXT, "a") as f:
        if event_type == "interval_start":
            f.write(f"\n[{ts_str}] === New Interval: {data.get('slug','')} | Bankroll: ${data.get('bankroll',0):.2f} ===\n")
        elif event_type == "open_price":
            f.write(f"[{ts_str}] Open: ${data.get('price',0):,.2f}\n")
        elif event_type == "entry":
            f.write(f"[{ts_str}] ENTRY: {data.get('strength','')} {data.get('side','')} @ {data.get('entry_price',0):.3f} | BTC ${data.get('btc_at_entry',0):,.2f} ({data.get('move_at_entry',0):+.3f}%) | Edge: {data.get('edge',0):+.3f} | {data.get('elapsed',0):.0f}s in\n")
        elif event_type == "resolve":
            result = "WIN" if data.get("won") else "LOSS"
            f.write(f"[{ts_str}] {result}: BTC ${data.get('btc_open',0):,.2f} -> ${data.get('btc_close',0):,.2f} | P&L ${data.get('pnl',0):+.2f} | Bankroll: ${data.get('bankroll',0):.2f}\n")
        elif event_type == "signal_skip":
            f.write(f"[{ts_str}] SKIP: {data.get('strength','')} {data.get('signal','')} @ {data.get('our_price',0):.3f} | edge {data.get('edge',0):+.3f} | move {data.get('move',0):+.3f}%\n")
        elif event_type == "error" or event_type == "fatal":
            f.write(f"[{ts_str}] ERROR: {data.get('error','')}\n")


def fmt_et(ts):
    return datetime.fromtimestamp(ts, tz=ET).strftime("%I:%M:%S %p")


def fmt_et_short(ts):
    return datetime.fromtimestamp(ts, tz=ET).strftime("%I:%M %p")


# ── Interval State ──────────────────────────────────────────
class IntervalState:
    def __init__(self, start_ts):
        self.start_ts = start_ts
        self.end_ts = start_ts + 900
        self.open_price = None
        self.latest_price = None
        self.high_price = None
        self.low_price = None
        self.trade_count = 0
        self.trade_taken = False
        self.trade = None

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

    @property
    def slug(self):
        return f"btc-updown-15m-{self.start_ts}"


# ── Paper Trader ────────────────────────────────────────────
class PaperTrader:
    def __init__(self):
        ensure_dirs()
        self.trades, self.bankroll = load_state()
        self.current_interval = None
        self.feed = BinanceFeed(symbols=["BTC"], on_trade=self._on_trade)
        self._session = None
        self._last_status_bucket = 0
        self._pending_resolve = None  # (IntervalState) queued for resolution

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

            print(f"\n--- {fmt_et_short(ts)}-{fmt_et_short(ts+900)} ET | ${self.bankroll:.2f} | {len(self.trades)} trades ---")
            log_event("interval_start", {"slug": self.current_interval.slug, "bankroll": self.bankroll})

            # Queue previous trade for resolution (handled in _on_trade)
            if prev and prev.trade:
                self._pending_resolve = prev

    async def _on_trade(self, symbol, price, exchange_ts, local_ts):
        try:
            await self._on_trade_inner(symbol, price, exchange_ts, local_ts)
        except Exception as e:
            log_event("error", {"context": "on_trade", "error": str(e)})
            print(f"  [ERR] {e}")

    async def _on_trade_inner(self, symbol, price, exchange_ts, local_ts):
        self._rotate_interval()

        # Resolve queued trade (now safely in async context)
        if self._pending_resolve:
            prev = self._pending_resolve
            self._pending_resolve = None
            await self._resolve(prev)

        iv = self.current_interval

        if iv.open_price is None:
            iv.open_price = price
            iv.high_price = price
            iv.low_price = price
            log_event("open_price", {"slug": iv.slug, "price": price})

        iv.latest_price = price
        iv.trade_count += 1
        if price > (iv.high_price or 0):
            iv.high_price = price
        if price < (iv.low_price or float('inf')):
            iv.low_price = price

        # Status every 60s (less spam)
        bucket = int(iv.elapsed) // 60
        if bucket > self._last_status_bucket and iv.elapsed > 10:
            self._last_status_bucket = bucket
            tag = f" [{iv.trade['side']}]" if iv.trade_taken else ""
            print(f"  {iv.move_pct:+.3f}% | ${price:,.0f} | {iv.remaining:.0f}s left{tag}")

        # Signal logic
        if iv.trade_taken:
            return
        elapsed = iv.elapsed
        if elapsed < ENTRY_WINDOW[0] or elapsed > ENTRY_WINDOW[1]:
            return

        abs_move = abs(iv.move_pct)
        if abs_move >= STRONG_MOVE_PCT:
            strength = "STRONG"
        elif abs_move >= MIN_MOVE_PCT and elapsed > 420:
            strength = "MODERATE"
        else:
            return

        signal = "Up" if iv.move_pct > 0 else "Down"
        await self._evaluate(iv, signal, strength, price)

    async def _evaluate(self, iv, signal, strength, btc_price):
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            market = await self._fetch_market(iv.slug)
            if not market:
                return

            prices = json.loads(market.get("outcomePrices", "[]"))
            if len(prices) < 2:
                return

            price_up, price_down = float(prices[0]), float(prices[1])
            our_price = price_up if signal == "Up" else price_down

            # Fair value estimate
            if strength == "STRONG":
                fair = 0.80 if iv.elapsed > 600 else 0.70
            else:
                fair = 0.65

            edge = fair - our_price

            if our_price > 0.75 or edge < 0.05:
                log_event("signal_skip", {
                    "slug": iv.slug, "signal": signal, "strength": strength,
                    "our_price": our_price, "edge": edge, "move": iv.move_pct
                })
                return

            # Take the trade
            shares = BET_SIZE / our_price
            iv.trade_taken = True
            iv.trade = {
                "side": signal,
                "entry_price": our_price,
                "shares": shares,
                "cost": BET_SIZE,
                "btc_at_entry": btc_price,
                "btc_open": iv.open_price,
                "move_at_entry": iv.move_pct,
                "strength": strength,
                "elapsed": iv.elapsed,
                "slug": iv.slug,
                "market_up": price_up,
                "market_down": price_down,
                "edge": edge,
                "ts": time.time(),
            }
            self.bankroll -= BET_SIZE
            save_state(self.trades, self.bankroll)

            print(f"  >> {strength} {signal} @ {our_price:.2f} | edge {edge:+.2f} | ${BET_SIZE} -> {shares:.1f} shares")
            log_event("entry", iv.trade)

        except Exception as e:
            log_event("error", {"context": "evaluate", "error": str(e)})

    async def _resolve(self, iv):
        """Resolve a completed interval's trade."""
        if not iv.trade:
            return
        trade = iv.trade

        try:
            went_up = iv.latest_price >= iv.open_price
            winner = "Up" if went_up else "Down"
            won = trade["side"] == winner
            payout = trade["shares"] if won else 0
            pnl = payout - trade["cost"]
            self.bankroll += payout

            trade["winner"] = winner
            trade["won"] = won
            trade["pnl"] = round(pnl, 2)
            trade["payout"] = round(payout, 2)
            trade["btc_close"] = iv.latest_price
            trade["btc_high"] = iv.high_price
            trade["btc_low"] = iv.low_price
            trade["final_move"] = ((iv.latest_price - iv.open_price) / iv.open_price) * 100

            self.trades.append(trade)
            save_state(self.trades, self.bankroll)

            wins = sum(1 for t in self.trades if t.get("won"))
            result = "WIN" if won else "LOSS"
            print(f"  << {result} | {trade['side']} | BTC {trade['final_move']:+.3f}% | P&L ${pnl:+.2f} | Bank ${self.bankroll:.2f} | {wins}/{len(self.trades)}")
            log_event("resolve", {
                "slug": trade["slug"], "won": won, "pnl": pnl,
                "bankroll": self.bankroll, "winner": winner,
                "btc_open": iv.open_price, "btc_close": iv.latest_price
            })

        except Exception as e:
            log_event("error", {"context": "resolve", "error": str(e)})

    async def _fetch_market(self, slug):
        url = f"{GAMMA_API}/markets"
        try:
            async with self._session.get(url, params={"slug": slug}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data[0] if data else None
        except Exception:
            return None

    async def run(self):
        wins = sum(1 for t in self.trades if t.get("won"))
        print(f"Paper Trader | BTC 15-Min | ${self.bankroll:.2f} | {wins}/{len(self.trades)} trades")
        print(f"Bet: ${BET_SIZE} | Min move: {MIN_MOVE_PCT}% | Window: {ENTRY_WINDOW[0]}-{ENTRY_WINDOW[1]}s")
        print(f"Logs: {TRADES_FILE}")
        await self.feed.start()

    def summary(self):
        wins = sum(1 for t in self.trades if t.get("won"))
        total_pnl = sum(t.get("pnl", 0) for t in self.trades)
        print(f"\n=== Summary: {wins}/{len(self.trades)} wins | P&L ${total_pnl:+.2f} | Bank ${self.bankroll:.2f} ===")


if __name__ == "__main__":
    import traceback
    trader = PaperTrader()
    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        trader.summary()
    except Exception as e:
        msg = traceback.format_exc()
        print(f"\n[FATAL] {msg}")
        log_event("fatal", {"error": str(e), "traceback": msg})
        trader.summary()
