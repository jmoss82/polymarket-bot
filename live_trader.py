"""
Live trader for BTC 15-min Up/Down markets.
Same strategy as paper_trader, but places real orders via py-clob-client.

Usage: python -u live_trader.py
Stop:  Ctrl+C (prints session summary)

REQUIRES .env with:
  POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE, POLY_PRIVATE_KEY, POLY_FUNDER
"""
import asyncio
import json
import os
import time
import traceback
from datetime import datetime, timezone, timedelta
from binance_ws import BinanceFeed
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from config import (
    POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE,
    POLY_PRIVATE_KEY, POLY_FUNDER, CHAIN_ID, CLOB_HOST,
)
import aiohttp

# ── Config ──────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
BET_SIZE = 5.0           # dollars per trade — start small
MIN_MOVE_PCT = 0.10
STRONG_MOVE_PCT = 0.15
ENTRY_WINDOW = (300, 840)  # enter between 5:00 and 14:00

DATA_DIR = "data"
TRADES_FILE = os.path.join(DATA_DIR, "live_trades.json")
LOG_FILE = os.path.join(DATA_DIR, "live_log.jsonl")
TRADES_CSV = os.path.join(DATA_DIR, "live_trades.csv")
TRADES_TXT = os.path.join(DATA_DIR, "live_log.txt")

ET = timezone(timedelta(hours=-5))
BANKROLL_START = 0  # will be read from account


# ── Helpers ─────────────────────────────────────────────────
def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def fmt_et(ts):
    return datetime.fromtimestamp(ts, tz=ET).strftime("%I:%M:%S %p")


def fmt_et_short(ts):
    return datetime.fromtimestamp(ts, tz=ET).strftime("%I:%M %p")


def load_state():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            state = json.load(f)
            return state.get("trades", []), state.get("bankroll", 0)
    return [], 0


def save_state(trades, bankroll):
    with open(TRADES_FILE, "w") as f:
        json.dump({"trades": trades, "bankroll": bankroll, "updated": time.time()}, f, indent=2)
    _write_csv(trades, bankroll)


def _write_csv(trades, bankroll):
    import csv
    headers = ["#", "Time (ET)", "Interval", "Side", "Strength", "Entry Price",
               "Move at Entry", "BTC Open", "BTC Close", "Final Move",
               "Result", "P&L", "Bankroll", "Order ID"]
    with open(TRADES_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for i, t in enumerate(trades, 1):
            w.writerow([
                i,
                fmt_et(t.get("ts", 0)),
                t.get("slug", ""),
                t.get("side", ""),
                t.get("strength", ""),
                f"{t.get('entry_price', 0):.3f}",
                f"{t.get('move_at_entry', 0):+.3f}%",
                f"${t.get('btc_open', 0):,.2f}",
                f"${t.get('btc_close', 0):,.2f}" if t.get("btc_close") else "",
                f"{t.get('final_move', 0):+.3f}%" if t.get("final_move") is not None else "",
                "WIN" if t.get("won") else "LOSS" if t.get("won") is not None else "OPEN",
                f"${t.get('pnl', 0):+.2f}" if t.get("pnl") is not None else "",
                f"${bankroll:.2f}",
                t.get("order_id", ""),
            ])


def log_event(event_type, data):
    entry = {"ts": time.time(), "type": event_type, **data}
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    ts_str = fmt_et(time.time())
    with open(TRADES_TXT, "a") as f:
        if event_type == "interval_start":
            f.write(f"\n[{ts_str}] === New Interval: {data.get('slug','')} | Bankroll: ${data.get('bankroll',0):.2f} ===\n")
        elif event_type == "open_price":
            f.write(f"[{ts_str}] Open: ${data.get('price',0):,.2f}\n")
        elif event_type == "entry":
            f.write(f"[{ts_str}] ENTRY: {data.get('strength','')} {data.get('side','')} @ {data.get('entry_price',0):.3f} | BTC ${data.get('btc_at_entry',0):,.2f} ({data.get('move_at_entry',0):+.3f}%) | Edge: {data.get('edge',0):+.3f} | Order: {data.get('order_id','?')}\n")
        elif event_type == "resolve":
            result = "WIN" if data.get("won") else "LOSS"
            f.write(f"[{ts_str}] {result}: P&L ${data.get('pnl',0):+.2f} | Bankroll: ${data.get('bankroll',0):.2f}\n")
        elif event_type == "order_error":
            f.write(f"[{ts_str}] ORDER ERROR: {data.get('error','')}\n")
        elif event_type in ("error", "fatal"):
            f.write(f"[{ts_str}] ERROR: {data.get('error','')}\n")


# ── Interval State ──────────────────────────────────────────
class IntervalState:
    def __init__(self, start_ts):
        self.start_ts = start_ts
        self.end_ts = start_ts + 900
        self.open_price = None
        self.latest_price = None
        self.high_price = None
        self.low_price = None
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


# ── Live Trader ─────────────────────────────────────────────
class LiveTrader:
    def __init__(self, single=False):
        ensure_dirs()
        self.trades, self.bankroll = load_state()
        self.current_interval = None
        self.feed = BinanceFeed(symbols=["BTC"], on_trade=self._on_trade)
        self._http = None
        self._last_status_bucket = 0
        self._pending_resolve = None
        self._single = single        # single trade mode
        self._trade_placed = False    # have we placed our one trade?
        self._resolved = False        # has it resolved?

        # Init CLOB client — derive fresh API creds on startup
        # (Polymarket L2 keys are IP-bound, so we re-derive each deploy)
        self.clob = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=POLY_PRIVATE_KEY,
            signature_type=0,
        )
        print("Deriving API credentials...", flush=True)
        try:
            creds = self.clob.derive_api_key()
            print(f"Derived creds type: {type(creds)}", flush=True)
            print(f"Derived creds: {creds}", flush=True)

            # Handle both dict and object responses
            if isinstance(creds, dict):
                api_key = creds.get("apiKey") or creds.get("api_key")
                api_secret = creds.get("secret") or creds.get("api_secret")
                api_passphrase = creds.get("passphrase") or creds.get("api_passphrase")
            else:
                api_key = creds.api_key
                api_secret = creds.api_secret
                api_passphrase = creds.api_passphrase

            self.clob = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=POLY_PRIVATE_KEY,
                creds=ApiCreds(api_key, api_secret, api_passphrase),
                funder=POLY_FUNDER,
                signature_type=0,
            )
            print(f"API creds derived OK (key: {api_key[:12]}...)", flush=True)

            # Quick auth check
            try:
                ok = self.clob.get_api_keys()
                print(f"Auth check: {ok}", flush=True)
            except Exception as ae:
                print(f"Auth check failed: {ae}", flush=True)
        except Exception as e:
            print(f"WARNING: Could not derive creds: {e}", flush=True)
            print("Falling back to env creds...", flush=True)
            self.clob = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=POLY_PRIVATE_KEY,
                creds=ApiCreds(POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE),
                funder=POLY_FUNDER,
                signature_type=0,
            )

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
        if price > (iv.high_price or 0):
            iv.high_price = price
        if price < (iv.low_price or float('inf')):
            iv.low_price = price

        # Status every 60s
        bucket = int(iv.elapsed) // 60
        if bucket > self._last_status_bucket and iv.elapsed > 10:
            self._last_status_bucket = bucket
            tag = f" [{iv.trade['side']}]" if iv.trade_taken else ""
            print(f"  {iv.move_pct:+.3f}% | ${price:,.0f} | {iv.remaining:.0f}s left{tag}")

        # Signal logic
        if iv.trade_taken or iv.elapsed < ENTRY_WINDOW[0] or iv.elapsed > ENTRY_WINDOW[1]:
            return
        if self._single and self._trade_placed:
            return

        abs_move = abs(iv.move_pct)
        if abs_move >= STRONG_MOVE_PCT:
            strength = "STRONG"
        elif abs_move >= MIN_MOVE_PCT and iv.elapsed > 420:
            strength = "MODERATE"
        else:
            return

        signal = "Up" if iv.move_pct > 0 else "Down"
        await self._evaluate(iv, signal, strength, price)

    async def _evaluate(self, iv, signal, strength, btc_price):
        try:
            if self._http is None or self._http.closed:
                self._http = aiohttp.ClientSession()

            market = await self._fetch_market(iv.slug)
            if not market:
                return

            prices = json.loads(market.get("outcomePrices", "[]"))
            clob_ids = json.loads(market.get("clobTokenIds", "[]"))
            if len(prices) < 2 or len(clob_ids) < 2:
                return

            price_up, price_down = float(prices[0]), float(prices[1])
            our_price = price_up if signal == "Up" else price_down
            token_id = clob_ids[0] if signal == "Up" else clob_ids[1]

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

            # Place real order
            order_id = await self._place_order(token_id, our_price, BET_SIZE)

            if not order_id:
                return

            iv.trade_taken = True
            iv.trade = {
                "side": signal,
                "entry_price": our_price,
                "shares": BET_SIZE / our_price,
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
                "token_id": token_id,
                "order_id": order_id,
                "ts": time.time(),
            }
            self.bankroll -= BET_SIZE
            save_state(self.trades, self.bankroll)

            self._trade_placed = True
            print(f"  >> LIVE {strength} {signal} @ {our_price:.2f} | edge {edge:+.2f} | order {order_id[:16]}...", flush=True)
            log_event("entry", {**iv.trade})

        except Exception as e:
            log_event("error", {"context": "evaluate", "error": str(e)})
            print(f"  [ERR evaluate] {e}")

    async def _place_order(self, token_id, price, amount):
        """Place a market buy order. Returns order_id or None."""
        try:
            # Build order — buying YES/NO tokens at limit price
            size = round(amount / price, 2)
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
            print(f"  [ORDER] token={token_id[:16]}... price={price} size={size} sig_type={self.clob.signature_type} funder={self.clob.funder}", flush=True)

            # Create and sign the order (sync — runs in executor)
            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(
                None, lambda: self.clob.create_and_post_order(order_args)
            )

            if signed_order and signed_order.get("orderID"):
                return signed_order["orderID"]
            elif signed_order and signed_order.get("success") is False:
                log_event("order_error", {"error": str(signed_order), "token_id": token_id})
                print(f"  [ORDER REJECTED] {signed_order}")
                return None
            else:
                # Some versions return differently
                order_id = str(signed_order) if signed_order else None
                return order_id

        except Exception as e:
            log_event("order_error", {"error": str(e), "token_id": token_id})
            print(f"  [ORDER ERROR] {e}")
            return None

    async def _resolve(self, iv):
        """Resolve a completed interval's trade using Binance price (same as paper)."""
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
                "order_id": trade.get("order_id"),
            })

            if self._single:
                self._resolved = True
                self.feed.stop()
                print("\n  Single trade complete. Stopping.", flush=True)

        except Exception as e:
            log_event("error", {"context": "resolve", "error": str(e)})

    async def _fetch_market(self, slug):
        url = f"{GAMMA_API}/markets"
        try:
            async with self._http.get(url, params={"slug": slug}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data[0] if data else None
        except Exception:
            return None

    async def run(self):
        # Verify credentials
        missing = []
        if not POLY_API_KEY: missing.append("POLY_API_KEY")
        if not POLY_API_SECRET: missing.append("POLY_API_SECRET")
        if not POLY_API_PASSPHRASE: missing.append("POLY_API_PASSPHRASE")
        if not POLY_PRIVATE_KEY: missing.append("POLY_PRIVATE_KEY")
        if missing:
            print(f"FATAL: Missing credentials: {', '.join(missing)}")
            print("Set them in .env or as environment variables.")
            return

        wins = sum(1 for t in self.trades if t.get("won"))
        print(f"LIVE Trader | BTC 15-Min | ${self.bankroll:.2f} | {wins}/{len(self.trades)} trades")
        print(f"Bet: ${BET_SIZE} | Min move: {MIN_MOVE_PCT}% | Window: {ENTRY_WINDOW[0]}-{ENTRY_WINDOW[1]}s")
        print(f"Logs: {TRADES_CSV}")
        print(f"*** REAL MONEY MODE ***")
        await self.feed.start()

    def summary(self):
        wins = sum(1 for t in self.trades if t.get("won"))
        total_pnl = sum(t.get("pnl", 0) for t in self.trades)
        print(f"\n=== LIVE Summary: {wins}/{len(self.trades)} wins | P&L ${total_pnl:+.2f} | Bank ${self.bankroll:.2f} ===")


if __name__ == "__main__":
    import sys
    single = "--single" in sys.argv
    trader = LiveTrader(single=single)
    if single:
        print("*** SINGLE TRADE MODE — will exit after one round trip ***", flush=True)
    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        msg = traceback.format_exc()
        print(f"\n[FATAL] {msg}")
        log_event("fatal", {"error": str(e), "traceback": msg})
    trader.summary()
