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
from chainlink_ws import ChainlinkFeed
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, BalanceAllowanceParams, AssetType
from config import (
    POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE,
    POLY_PRIVATE_KEY, POLY_FUNDER, CHAIN_ID, CLOB_HOST,
)
import aiohttp

# ── Config ──────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"

def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)

def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)

BET_SIZE = _env_float("BET_SIZE", 5.0)           # dollars per trade — start small
MIN_MOVE_PCT = _env_float("MIN_MOVE_PCT", 0.05)  # TEMP: lowered from 0.10 to test order execution
STRONG_MOVE_PCT = _env_float("STRONG_MOVE_PCT", 0.10)   # TEMP: lowered from 0.15
ENTRY_WINDOW = (
    _env_int("ENTRY_START", 45),
    _env_int("ENTRY_END", 840),
)
MIN_EDGE = _env_float("MIN_EDGE", 0.02)
MAX_ENTRY_PRICE = _env_float("MAX_ENTRY_PRICE", 0.75)

# Exit logic
TAKE_PROFIT_PCT = _env_float("TAKE_PROFIT_PCT", 0.50)   # sell when position is up +50%
STOP_LOSS_PCT = _env_float("STOP_LOSS_PCT", 0.25)       # sell when position is down -25%
EXIT_BEFORE_END = _env_int("EXIT_BEFORE_END", 60)        # forced sell with 60s remaining
MONITOR_INTERVAL = _env_int("MONITOR_INTERVAL", 5)       # check price every 5 seconds

# Risk management
MAX_SESSION_LOSS = _env_float("MAX_SESSION_LOSS", 15.0)  # stop trading after $15 cumulative loss
MAX_SPREAD = _env_float("MAX_SPREAD", 0.06)              # skip entry if spread > 6 cents
ENTRY_ORDER_TIMEOUT = _env_float("ENTRY_ORDER_TIMEOUT", 20.0)  # seconds to wait for entry fill
EXIT_ORDER_TIMEOUT = _env_float("EXIT_ORDER_TIMEOUT", 20.0)    # seconds to wait for exit fill

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
        elif event_type == "exit":
            f.write(f"[{ts_str}] EXIT ({data.get('reason','')}): sold @ {data.get('exit_price',0):.3f} | P&L ${data.get('pnl',0):+.2f} | Bankroll: ${data.get('bankroll',0):.2f}\n")
        elif event_type in ("sell_error",):
            f.write(f"[{ts_str}] SELL ERROR: {data.get('error','')} (reason: {data.get('reason','')})\n")
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
        self.last_log_minute = -1       # throttle below-threshold signal logging (per minute)
        self.last_eval_half_min = -1    # throttle entry evaluations (per 30s)
        # Exit tracking
        self.exited = False
        self.exit_reason = None      # "take_profit", "stop_loss", "forced_exit"
        self.exit_price = None
        self.exit_pnl = None
        self.last_monitor_ts = 0     # throttle Polymarket price checks
        self.sell_attempts = 0       # cap retries on failed sells

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
    def __init__(self, single=False, audit=False):
        ensure_dirs()
        self.trades, self.bankroll = load_state()
        self.current_interval = None
        # Price feed: Chainlink (via Polymarket RTDS) is the default because
        # it's the same oracle Polymarket uses to resolve BTC 15-min markets.
        # Set PRICE_FEED=binance to fall back to Binance if needed.
        feed_choice = os.getenv("PRICE_FEED", "chainlink").lower()
        if feed_choice == "binance":
            self.feed = BinanceFeed(symbols=["BTC"], on_trade=self._on_trade)
            self._feed_name = "Binance"
        else:
            self.feed = ChainlinkFeed(symbols=["BTC"], on_trade=self._on_trade)
            self._feed_name = "Chainlink"
        self._http = None
        self._last_status_bucket = 0
        self._pending_resolve = None
        self._single = single        # single trade mode
        self._trade_placed = False    # have we placed our one trade?
        self._resolved = False        # has it resolved?
        self._audit = audit

        # Runtime config (can be overridden for audit mode)
        self.bet_size = BET_SIZE
        self.min_move_pct = MIN_MOVE_PCT
        self.strong_move_pct = STRONG_MOVE_PCT
        self.entry_window = ENTRY_WINDOW
        self.min_edge = MIN_EDGE
        self.max_entry_price = MAX_ENTRY_PRICE
        self.take_profit_pct = TAKE_PROFIT_PCT
        self.stop_loss_pct = STOP_LOSS_PCT
        self.exit_before_end = EXIT_BEFORE_END
        self.monitor_interval = MONITOR_INTERVAL
        self.max_session_loss = MAX_SESSION_LOSS
        self.max_spread = MAX_SPREAD
        self.entry_order_timeout = ENTRY_ORDER_TIMEOUT
        self.exit_order_timeout = EXIT_ORDER_TIMEOUT
        self.session_pnl = 0.0          # tracks cumulative P&L this session
        self._circuit_breaker = False    # set True when max loss hit

        if self._audit:
            self._apply_audit_overrides()

        # Init CLOB client — derive fresh API creds on startup
        # (Polymarket L2 keys are IP-bound, so we re-derive each deploy)
        # signature_type=2 (POLY_GNOSIS_SAFE) for MetaMask proxy wallet accounts
        self.clob = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=POLY_PRIVATE_KEY,
            signature_type=2,
        )
        print("Deriving API credentials...", flush=True)
        try:
            creds = self.clob.derive_api_key()

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
                signature_type=2,
            )
            print(f"API creds derived OK (key: {api_key[:12]}...)", flush=True)

            # Quick auth check
            try:
                keys = self.clob.get_api_keys()
                print(f"Auth check: {keys}", flush=True)
            except Exception as ae:
                print(f"Auth check failed: {ae}", flush=True)

            # Refresh CLOB allowance cache for both USDC (buys) and Conditional Tokens (sells).
            # This does NOT set on-chain approvals — run set_allowances.py for that.
            # It tells the CLOB server to re-read on-chain state so it knows we have approval.
            self._refresh_allowances()
        except Exception as e:
            print(f"WARNING: Could not derive creds: {e}", flush=True)
            print("Falling back to env creds...", flush=True)
            self.clob = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=POLY_PRIVATE_KEY,
                creds=ApiCreds(POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE),
                funder=POLY_FUNDER,
                signature_type=2,
            )

    def _refresh_allowances(self):
        """Tell the CLOB server to refresh its cached view of on-chain allowances.

        Polymarket's CLOB caches your on-chain token approvals.  After a buy,
        the conditional-token balance changes, but the server may not see it
        until we explicitly ask it to refresh.  Without this, sell orders fail
        with 'not enough balance / allowance'.

        This is a server-side cache refresh — NOT an on-chain transaction.
        On-chain approvals (setApprovalForAll) must be set once via
        set_allowances.py or the Polymarket UI.
        """
        for asset_label, asset_type in [("USDC", AssetType.COLLATERAL),
                                         ("Conditional Tokens", AssetType.CONDITIONAL)]:
            try:
                params = BalanceAllowanceParams(asset_type=asset_type)
                result = self.clob.update_balance_allowance(params)
                print(f"  Allowance refresh ({asset_label}): {result}", flush=True)
            except Exception as e:
                print(f"  Allowance refresh ({asset_label}) failed: {e}", flush=True)

    def _log_balance(self):
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.clob.get_balance_allowance(params)
            print(f"USDC balance/allowance: {bal}", flush=True)
            log_event("balance", {"balance": bal})
        except Exception as e:
            print(f"Balance check failed: {e}", flush=True)
            log_event("error", {"context": "balance", "error": str(e)})

        # Also check conditional token allowance — this is what enables sells
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
            bal = self.clob.get_balance_allowance(params)
            print(f"Conditional token allowance: {bal}", flush=True)
            log_event("conditional_allowance", {"balance": bal})
        except Exception as e:
            print(f"Conditional token allowance check failed: {e}", flush=True)

    def _log_and_check_funder(self):
        """Log wallet addresses and verify POLY_FUNDER is set."""
        try:
            from eth_account import Account
        except Exception as e:
            print(f"WARNING: Cannot import eth_account to verify funder address: {e}", flush=True)
            if not POLY_FUNDER:
                print("FATAL: POLY_FUNDER is missing.", flush=True)
                return False
            return True

        try:
            derived = Account.from_key(POLY_PRIVATE_KEY).address
            print(f"EOA (signer): {derived}", flush=True)
            print(f"Funder:       {POLY_FUNDER}", flush=True)
            log_event("wallet", {"derived_address": derived, "funder": POLY_FUNDER})

            if not POLY_FUNDER:
                print("FATAL: POLY_FUNDER is missing.", flush=True)
                return False

            if derived.lower() != POLY_FUNDER.lower():
                print("Funder != EOA (proxy wallet mode)", flush=True)

            return True
        except Exception as e:
            print(f"FATAL: Could not derive address from POLY_PRIVATE_KEY: {e}", flush=True)
            return False

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
            if iv.exited and iv.trade:
                tag = f" [{iv.trade['side']}] EXITED ({iv.exit_reason})"
            elif iv.trade_taken and iv.trade:
                tag = f" [{iv.trade['side']}] OPEN"
            elif iv.trade_taken:
                tag = " [no fill]"
            else:
                tag = ""
            print(f"  {iv.move_pct:+.3f}% | ${price:,.0f} | {iv.remaining:.0f}s left{tag}")

        # Monitor open position for TP/SL/forced exit
        if iv.trade_taken and iv.trade and not iv.exited:
            await self._monitor_position(iv)

        # Circuit breaker — stop trading if max session loss exceeded
        if self._circuit_breaker:
            return

        # Signal logic — skip if already traded or exited, or outside entry window
        if iv.trade_taken or iv.elapsed < self.entry_window[0] or iv.elapsed > self.entry_window[1]:
            return
        if self._single and self._trade_placed:
            return

        abs_move = abs(iv.move_pct)
        if abs_move >= self.strong_move_pct:
            strength = "STRONG"
        elif abs_move >= self.min_move_pct and iv.elapsed > 420:
            strength = "MODERATE"
        else:
            if abs_move > 0.03:
                minute = int(iv.elapsed) // 60
                if minute != iv.last_log_minute:
                    iv.last_log_minute = minute
                    print(f"  [SIGNAL] {abs_move:.3f}% @ {iv.elapsed:.0f}s — below threshold", flush=True)
            return

        # Throttle evaluations to once per 30 seconds (balance API load vs responsiveness)
        half_min = int(iv.elapsed) // 30
        if half_min == iv.last_eval_half_min:
            return
        iv.last_eval_half_min = half_min

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
            outcomes = json.loads(market.get("outcomes", "[]"))
            if len(prices) < 2 or len(clob_ids) < 2 or len(outcomes) < 2:
                return

            # Map outcomes to indices — never assume ordering
            outcome_map = {}  # {"Up": 0, "Down": 1} or reversed
            for i, name in enumerate(outcomes):
                outcome_map[name] = i

            if signal not in outcome_map:
                print(f"  [SKIP] Signal '{signal}' not in outcomes {outcomes}", flush=True)
                return

            sig_idx = outcome_map[signal]
            price_up = float(prices[outcome_map.get("Up", 0)])
            price_down = float(prices[outcome_map.get("Down", 1)])
            token_id = clob_ids[sig_idx]
            accepting = market.get("acceptingOrders")
            min_size = market.get("orderMinSize")

            if accepting is False:
                log_event("signal_skip", {
                    "slug": iv.slug, "signal": signal, "strength": strength,
                    "reason": "acceptingOrders=false",
                })
                return

            # Fetch live orderbook from CLOB (not stale Gamma prices)
            best_bid, best_ask, bid_depth, ask_depth, spread = await self._get_order_book(token_id)
            if best_ask is None or best_bid is None:
                print(f"  [SKIP] No orderbook data for {signal}", flush=True)
                return

            # Actual buy price: we place a GTC limit at best_ask + 0.01 to fill aggressively.
            # Edge must be calculated against THIS price, not bare best_ask.
            buy_price = round(min(best_ask + 0.01, 0.99), 2)
            our_price = buy_price

            # Spread filter — skip if spread is too wide (eating our edge)
            if spread > self.max_spread:
                print(f"  [SKIP] Spread {spread:.3f} > max {self.max_spread:.3f} | bid {best_bid:.3f} / ask {best_ask:.3f}", flush=True)
                log_event("signal_skip", {
                    "slug": iv.slug, "signal": signal, "strength": strength,
                    "spread": spread, "best_bid": best_bid, "best_ask": best_ask,
                    "reason": "spread_too_wide",
                })
                return

            if strength == "STRONG":
                fair = 0.85 if iv.elapsed > 600 else 0.75
            else:
                fair = 0.70

            edge = fair - our_price

            log_event("market_snapshot", {
                "slug": iv.slug,
                "signal": signal,
                "strength": strength,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "bid_depth": round(bid_depth, 2),
                "ask_depth": round(ask_depth, 2),
                "our_price": our_price,
                "edge": edge,
                "accepting_orders": accepting,
                "order_min_size": min_size,
            })

            if our_price > self.max_entry_price or edge < self.min_edge:
                print(f"  [SKIP] ask {best_ask:.3f} | edge {edge:+.3f} (need {self.min_edge}) | spread {spread:.3f}", flush=True)
                log_event("signal_skip", {
                    "slug": iv.slug, "signal": signal, "strength": strength,
                    "our_price": our_price, "edge": edge, "move": iv.move_pct,
                })
                return

            # buy_price already calculated above (best_ask + 0.01, capped at 0.99)
            print(f"  [BOOK] token={token_id[:16]}... bid {best_bid:.3f} / ask {best_ask:.3f} | spread {spread:.3f} | depth ${bid_depth:.0f}/${ask_depth:.0f}", flush=True)
            order_id = await self._place_order(token_id, buy_price, self.bet_size)

            if not order_id:
                # Order failed at submission (API error, not a fill issue).
                # Allow retry on the next evaluation cycle instead of locking
                # out the entire interval — the issue may be transient.
                log_event("order_error_retry", {"token_id": token_id, "reason": "no_order_id"})
                return

            # Order was accepted — now we're committed. Mark trade_taken to
            # prevent duplicate orders while we wait for fill confirmation.
            iv.trade_taken = True

            # Confirm fill — poll order status (don't assume filled just because we got an orderID)
            filled_size, fill_price = await self._confirm_fill(order_id, max_wait=self.entry_order_timeout)
            if filled_size <= 0:
                print(f"  [SKIP] Order {order_id[:16]}... not filled — no position", flush=True)
                log_event("order_unfilled", {"order_id": order_id, "token_id": token_id})
                return

            # Use actual fill data for position tracking
            actual_cost = round(filled_size * fill_price, 4)

            iv.trade = {
                "side": signal,
                "entry_price": fill_price,
                "market_price_at_entry": our_price,
                "limit_price": buy_price,
                "shares": filled_size,
                "cost": actual_cost,
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
            self.bankroll -= actual_cost
            save_state(self.trades, self.bankroll)

            self._trade_placed = True
            print(f"  >> FILLED {strength} {signal} | {filled_size} shares @ {fill_price:.3f} (limit {buy_price:.2f}) | cost ${actual_cost:.2f} | order {order_id[:16]}...", flush=True)
            log_event("entry", {**iv.trade})

        except Exception as e:
            log_event("error", {"context": "evaluate", "error": str(e)})
            print(f"  [ERR evaluate] {e}")

    async def _place_order(self, token_id, price, amount):
        """Place a GTC limit buy order. Returns order_id or None."""
        try:
            # GTC requires USDC cost (size * price) to have max 2 decimal places.
            price_cents = int(round(price * 100))
            raw_size_cents = int(amount * 100 * 100 // price_cents)

            size_cents = raw_size_cents
            while size_cents > 100 and (size_cents * price_cents) % 100 != 0:
                size_cents -= 1

            size = size_cents / 100.0
            if size * price < 1.0:
                size = round(1.05 / price, 2)

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
            print(f"  [ORDER] GTC BUY token={token_id[:16]}... price={price} size={size} (~${size * price:.2f})", flush=True)

            # Two-step: create standard order, then post with GTC.
            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(
                None, lambda: self.clob.create_order(order_args)
            )
            result = await loop.run_in_executor(
                None, lambda: self.clob.post_order(signed_order, OrderType.GTC)
            )

            if not result:
                print(f"  [ORDER] Empty response", flush=True)
                return None

            # Parse response
            error_msg = ""
            order_id = None
            status = ""
            if isinstance(result, dict):
                error_msg = result.get("errorMsg", "")
                order_id = result.get("orderID", "")
                status = result.get("status", "")
            else:
                order_id = str(result)

            if error_msg:
                print(f"  [ORDER] GTC rejected: {error_msg}", flush=True)
                log_event("order_error", {"error": error_msg, "token_id": token_id})
                return None

            if not order_id:
                print(f"  [ORDER] No order ID: {result}", flush=True)
                return None

            print(f"  [ORDER] GTC accepted: status={status} id={order_id[:16]}...", flush=True)
            return order_id

        except Exception as e:
            log_event("order_error", {"error": str(e), "token_id": token_id})
            print(f"  [ORDER ERROR] {e}")
            return None

    async def _confirm_fill(self, order_id, max_wait=20.0):
        """Poll fill details for an order. Returns (filled_size, avg_price) or (0, 0).
        If not filled by max_wait, attempts cancellation and returns (0, 0)."""
        loop = asyncio.get_event_loop()
        start = time.time()
        attempts = 0

        while time.time() - start < max_wait:
            attempts += 1
            try:
                order = await loop.run_in_executor(
                    None, lambda: self.clob.get_order(order_id)
                )

                if not order:
                    print(f"  [FILL] No order data for {order_id[:16]}... (attempt {attempts})", flush=True)
                    await asyncio.sleep(1.0)
                    continue

                # Handle both dict and object responses.
                # Prefer actual execution price over limit price when available.
                if isinstance(order, dict):
                    size_matched = float(order.get("size_matched", 0))
                    original_size = float(order.get("original_size", 0))
                    # Try known fields for actual fill price; fall back to limit price
                    price = float(
                        order.get("average_matched_price")
                        or order.get("matched_price")
                        or order.get("price", 0)
                    )
                    status = order.get("status", "unknown")
                else:
                    size_matched = float(getattr(order, "size_matched", 0))
                    original_size = float(getattr(order, "original_size", 0))
                    price = float(
                        getattr(order, "average_matched_price", None)
                        or getattr(order, "matched_price", None)
                        or getattr(order, "price", 0)
                    )
                    status = getattr(order, "status", "unknown")

                print(f"  [FILL] status={status} filled={size_matched}/{original_size}", flush=True)

                if size_matched > 0:
                    return size_matched, price
                if status in ("CANCELED", "CANCELLED", "EXPIRED"):
                    print(f"  [FILL] Order {status} — no fill", flush=True)
                    return 0, 0

                # MATCHED with size_matched=0 is likely API lag — poll a few
                # more times rather than immediately trusting it as a full fill.
                # This prevents phantom fills that corrupt bankroll tracking.
                if status == "MATCHED" and size_matched == 0 and attempts >= 5:
                    print(f"  [FILL] MATCHED but size_matched=0 after {attempts} polls — accepting as filled", flush=True)
                    return original_size, price

            except Exception as e:
                print(f"  [FILL] Check error: {e}", flush=True)
            await asyncio.sleep(1.0)

        # Timed out waiting; cancel to avoid resting stale orders.
        try:
            await loop.run_in_executor(None, lambda: self.clob.cancel(order_id))
            print(f"  [FILL] Timeout after {max_wait:.0f}s — canceled {order_id[:16]}...", flush=True)
        except Exception as e:
            print(f"  [FILL] Timeout cancel failed: {e}", flush=True)

        return 0, 0

    async def _get_order_book(self, token_id):
        """Get full orderbook from CLOB. Returns (best_bid, best_ask, bid_depth, ask_depth, spread) or Nones."""
        loop = asyncio.get_event_loop()
        try:
            book = await loop.run_in_executor(
                None, lambda: self.clob.get_order_book(token_id)
            )

            if not book:
                return None, None, None, None, None

            # Parse bids and asks — handle both dict and object
            if isinstance(book, dict):
                bids = book.get("bids", [])
                asks = book.get("asks", [])
            else:
                bids = getattr(book, "bids", []) or []
                asks = getattr(book, "asks", []) or []

            # Best bid = highest bid, best ask = lowest ask
            best_bid = 0
            bid_depth = 0
            for b in bids:
                p = float(b.get("price", 0) if isinstance(b, dict) else getattr(b, "price", 0))
                s = float(b.get("size", 0) if isinstance(b, dict) else getattr(b, "size", 0))
                if p > best_bid:
                    best_bid = p
                bid_depth += p * s  # dollar depth

            best_ask = 1.0
            ask_depth = 0
            for a in asks:
                p = float(a.get("price", 0) if isinstance(a, dict) else getattr(a, "price", 0))
                s = float(a.get("size", 0) if isinstance(a, dict) else getattr(a, "size", 0))
                if p < best_ask:
                    best_ask = p
                ask_depth += p * s

            spread = round(best_ask - best_bid, 4) if best_ask > best_bid else 0

            return best_bid, best_ask, bid_depth, ask_depth, spread

        except Exception as e:
            print(f"  [BOOK ERR] {e}", flush=True)
            return None, None, None, None, None

    async def _get_clob_midpoint(self, token_id):
        """Get live midpoint price from CLOB orderbook (not cached Gamma API)."""
        loop = asyncio.get_event_loop()
        mid = await loop.run_in_executor(
            None, lambda: self.clob.get_midpoint(token_id)
        )
        # Returns string like "0.55" or a dict — handle both
        if isinstance(mid, dict):
            return float(mid.get("mid", 0))
        return float(mid)

    async def _get_clob_price(self, token_id, side="SELL"):
        """Get live bid/ask price from CLOB orderbook."""
        loop = asyncio.get_event_loop()
        price = await loop.run_in_executor(
            None, lambda: self.clob.get_price(token_id, side)
        )
        if isinstance(price, dict):
            return float(price.get("price", 0))
        return float(price)

    async def _monitor_position(self, iv):
        """Check current market price via CLOB orderbook and decide whether to exit."""
        now = time.time()

        # Forced exit — sell no matter what with EXIT_BEFORE_END seconds remaining
        if iv.remaining <= self.exit_before_end:
            print(f"  [EXIT] Forced exit — {iv.remaining:.0f}s left before resolution", flush=True)
            await self._sell_position(iv, reason="forced_exit")
            return

        # Throttle CLOB price checks
        if now - iv.last_monitor_ts < self.monitor_interval:
            return
        iv.last_monitor_ts = now

        # Fetch live midpoint from CLOB orderbook (not Gamma)
        try:
            token_id = iv.trade["token_id"]
            current_price = await self._get_clob_midpoint(token_id)

            if not current_price or current_price <= 0:
                print(f"  [MON] No CLOB price for token {token_id[:16]}...", flush=True)
                return

            entry_price = iv.trade["entry_price"]
            position_pnl_pct = (current_price - entry_price) / entry_price

            # Log position status on each check
            print(f"  [MON] {iv.trade['side']} | entry {entry_price:.3f} -> {current_price:.3f} | P&L {position_pnl_pct:+.1%} | {iv.remaining:.0f}s left", flush=True)

            # Take profit — use actual bid for floor price, not midpoint
            if position_pnl_pct >= self.take_profit_pct:
                print(f"  [EXIT] Take profit: {position_pnl_pct:+.1%} (entry {entry_price:.3f} -> {current_price:.3f})", flush=True)
                await self._sell_position(iv, reason="take_profit")
                return

            # Stop loss — use actual bid for floor price, not midpoint
            if position_pnl_pct <= -self.stop_loss_pct:
                print(f"  [EXIT] Stop loss: {position_pnl_pct:+.1%} (entry {entry_price:.3f} -> {current_price:.3f})", flush=True)
                await self._sell_position(iv, reason="stop_loss")
                return

        except Exception as e:
            log_event("error", {"context": "monitor_position", "error": str(e)})
            print(f"  [ERR monitor] {e}", flush=True)

    async def _sell_position(self, iv, reason="manual", sell_price=None):
        """Sell the current position using an aggressive GTC limit order on the CLOB."""
        MAX_SELL_ATTEMPTS = 3
        iv.sell_attempts += 1
        if iv.sell_attempts > MAX_SELL_ATTEMPTS:
            print(f"  [SELL GAVE UP] {iv.sell_attempts - 1} failed attempts — marking exited", flush=True)
            iv.exited = True
            iv.exit_reason = f"{reason}_failed"
            iv.exit_pnl = 0  # unknown, couldn't sell
            log_event("sell_error", {"reason": reason, "error": f"gave up after {MAX_SELL_ATTEMPTS} attempts"})
            return

        trade = iv.trade
        token_id = trade["token_id"]
        shares = round(trade["shares"], 2)  # actual filled shares — never exceed this

        # Refresh CLOB's view of our conditional token balance/allowance before selling.
        # Without this, the CLOB may reject the sell with "not enough balance / allowance"
        # because its cache doesn't reflect tokens received from a recent buy.
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self.clob.update_balance_allowance(
                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
                )
            )
        except Exception as e:
            print(f"  [SELL] Allowance refresh failed (continuing): {e}", flush=True)

        # Get current bid price for PnL estimation and floor price
        if sell_price is None:
            try:
                sell_price = await self._get_clob_price(token_id, side="SELL")
                if sell_price and sell_price > 0:
                    print(f"  [SELL] CLOB bid price: {sell_price:.3f}", flush=True)
                else:
                    sell_price = None
            except Exception as e:
                print(f"  [SELL] CLOB price fetch failed: {e}", flush=True)
                sell_price = None

        if sell_price is None:
            # Last resort: sell at a discount to guarantee fill
            sell_price = max(trade["entry_price"] * 0.5, 0.01)
            print(f"  [WARN] Could not fetch sell price, using {sell_price:.3f}", flush=True)

        # Floor price for sell — worst price we'll accept
        floor_price = round(max(sell_price - 0.02, 0.01), 2)

        # Check if sell meets $1 minimum — NEVER inflate shares beyond what we own
        order_value = shares * floor_price
        if order_value < 1.0:
            print(f"  [SELL SKIP] Order value ${order_value:.2f} < $1 min (have {shares} shares @ {floor_price}) — cannot sell", flush=True)
            log_event("sell_error", {"reason": reason, "error": f"below $1 min: {shares} shares @ {floor_price}"})
            # On forced exit, mark as exited anyway to avoid infinite retries
            if reason == "forced_exit":
                iv.exited = True
                iv.exit_reason = "forced_exit_below_min"
                iv.exit_pnl = 0
            return

        try:
            # Aggressive GTC limit sell. If not filled by timeout, cancel and retry.
            order_args = OrderArgs(
                token_id=token_id,
                price=floor_price,
                size=shares,
                side="SELL",
            )
            print(f"  [SELL] GTC token={token_id[:16]}... price={floor_price} size={shares} reason={reason}", flush=True)

            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(
                None, lambda: self.clob.create_order(order_args)
            )
            result = await loop.run_in_executor(
                None, lambda: self.clob.post_order(signed_order, OrderType.GTC)
            )

            # Parse response
            error_msg = ""
            order_id = None
            if isinstance(result, dict):
                error_msg = result.get("errorMsg", "")
                order_id = result.get("orderID", "")
            else:
                order_id = str(result) if result else None

            if error_msg:
                print(f"  [SELL GTC REJECTED] {error_msg}", flush=True)
                log_event("sell_error", {"error": error_msg, "reason": reason})
                return

            if order_id:
                # Confirm fill details; timeout cancels stale order automatically.
                filled_size, fill_price = await self._confirm_fill(order_id, max_wait=self.exit_order_timeout)
                if filled_size <= 0:
                    print(f"  [SELL UNFILLED] {order_id[:16]}... after {self.exit_order_timeout:.0f}s", flush=True)
                    log_event("sell_error", {"reason": reason, "error": "unfilled_timeout", "order_id": order_id})
                    return
                exit_price = fill_price if fill_price > 0 else sell_price

                # Use actual filled_size for P&L — not trade["shares"] — in case of partial fill
                sold_shares = min(filled_size, trade["shares"])
                pnl_est = (exit_price - trade["entry_price"]) * sold_shares
                iv.exited = True
                iv.exit_reason = reason
                iv.exit_price = exit_price
                iv.exit_pnl = round(pnl_est, 2)

                # Credit the sale proceeds back (only for shares actually sold)
                proceeds = exit_price * sold_shares
                self.bankroll += round(proceeds, 2)

                trade["exit_price"] = exit_price
                trade["exit_reason"] = reason
                trade["exit_order_id"] = order_id
                trade["exit_ts"] = time.time()
                trade["exit_pnl"] = iv.exit_pnl

                save_state(self.trades, self.bankroll)

                print(f"  >> SOLD ({reason}) @ {exit_price:.3f} | P&L ${pnl_est:+.2f} | order {order_id[:16]}...", flush=True)
                log_event("exit", {
                    "slug": iv.slug, "reason": reason,
                    "entry_price": trade["entry_price"],
                    "exit_price": exit_price,
                    "pnl": iv.exit_pnl,
                    "bankroll": self.bankroll,
                    "order_id": order_id,
                })
            else:
                print(f"  [SELL FAILED] No order ID returned for {reason}", flush=True)
                log_event("sell_error", {"reason": reason, "error": "no order_id"})

        except Exception as e:
            log_event("sell_error", {"error": str(e), "reason": reason})
            print(f"  [SELL ERROR] {e}", flush=True)

    async def _resolve(self, iv):
        """Resolve a completed interval's trade.

        Two paths:
        1. Early exit (TP/SL/forced) — position already sold, just log final outcome.
        2. Hold to resolution — original behavior, settle based on BTC close.
        """
        if not iv.trade:
            return
        trade = iv.trade

        try:
            went_up = iv.latest_price >= iv.open_price
            winner = "Up" if went_up else "Down"
            trade["winner"] = winner
            trade["btc_close"] = iv.latest_price
            trade["btc_high"] = iv.high_price
            trade["btc_low"] = iv.low_price
            trade["final_move"] = ((iv.latest_price - iv.open_price) / iv.open_price) * 100

            if iv.exited:
                # Already sold — use the P&L from the sell
                pnl = iv.exit_pnl or 0
                won = pnl > 0
                trade["won"] = won
                trade["pnl"] = pnl
                trade["payout"] = round(trade["cost"] + pnl, 2)
                trade["exit_type"] = iv.exit_reason
                # Bankroll was already credited in _sell_position

                would_have_won = trade["side"] == winner
                result = "WIN" if won else "LOSS"
                held_result = "would've WON" if would_have_won else "would've LOST"
                print(f"  << {result} ({iv.exit_reason}) | P&L ${pnl:+.2f} | {held_result} if held | Bank ${self.bankroll:.2f}")
            else:
                # Held to resolution — original logic
                won = trade["side"] == winner
                payout = trade["shares"] if won else 0
                pnl = payout - trade["cost"]
                self.bankroll += payout

                trade["won"] = won
                trade["pnl"] = round(pnl, 2)
                trade["payout"] = round(payout, 2)
                trade["exit_type"] = "resolution"

                result = "WIN" if won else "LOSS"
                print(f"  << {result} | {trade['side']} | BTC {trade['final_move']:+.3f}% | P&L ${pnl:+.2f} | Bank ${self.bankroll:.2f} | {sum(1 for t in self.trades if t.get('won'))}/{len(self.trades)}")

            self.trades.append(trade)
            save_state(self.trades, self.bankroll)

            # Track session P&L and check circuit breaker
            self.session_pnl += pnl
            if self.session_pnl <= -self.max_session_loss and not self._circuit_breaker:
                self._circuit_breaker = True
                print(f"  *** CIRCUIT BREAKER *** Session P&L ${self.session_pnl:+.2f} hit max loss ${-self.max_session_loss:.2f} — no more trades", flush=True)
                log_event("circuit_breaker", {"session_pnl": self.session_pnl, "max_loss": self.max_session_loss})

            log_event("resolve", {
                "slug": trade["slug"], "won": won, "pnl": pnl,
                "bankroll": self.bankroll, "winner": winner,
                "exit_type": trade.get("exit_type"),
                "order_id": trade.get("order_id"),
                "session_pnl": self.session_pnl,
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
        mode = "AUDIT" if self._audit else "LIVE"
        print(f"{mode} Trader | BTC 15-Min | ${self.bankroll:.2f} | {wins}/{len(self.trades)} trades")
        print(f"Price feed: {self._feed_name} (set PRICE_FEED=binance to override)")
        print(f"Bet: ${self.bet_size} | Min move: {self.min_move_pct}% | Window: {self.entry_window[0]}-{self.entry_window[1]}s")
        print(f"Edge: >= {self.min_edge} | Max price: {self.max_entry_price}")
        print(f"Exit: TP {self.take_profit_pct:+.0%} / SL {-self.stop_loss_pct:+.0%} / Forced @ {self.exit_before_end}s before end | Monitor every {self.monitor_interval}s")
        print(f"Order fills: entry timeout {self.entry_order_timeout:.0f}s | exit timeout {self.exit_order_timeout:.0f}s")
        print(f"Risk: Max session loss ${self.max_session_loss:.0f} | Max spread: {self.max_spread:.2f}")
        print(f"Logs: {TRADES_CSV}")
        print(f"*** REAL MONEY MODE ***")
        if not self._log_and_check_funder():
            return
        self._log_balance()
        await self.feed.start()

    def _apply_audit_overrides(self):
        # Intentionally permissive to guarantee a single live fill quickly.
        # Min $5 bet to stay above Polymarket's $1 minimum order value at any price.
        self.bet_size = _env_float("AUDIT_BET_SIZE", 5.0)
        self.min_move_pct = _env_float("AUDIT_MIN_MOVE_PCT", 0.0)
        self.strong_move_pct = _env_float("AUDIT_STRONG_MOVE_PCT", 0.0)
        self.entry_window = (
            _env_int("AUDIT_ENTRY_START", 30),
            _env_int("AUDIT_ENTRY_END", 840),
        )
        self.min_edge = _env_float("AUDIT_MIN_EDGE", 0.0)
        self.max_entry_price = _env_float("AUDIT_MAX_ENTRY_PRICE", 0.98)
        # Tighter exits for audit — quicker TP/SL to finish the round trip fast
        self.take_profit_pct = _env_float("AUDIT_TAKE_PROFIT_PCT", 0.15)
        self.stop_loss_pct = _env_float("AUDIT_STOP_LOSS_PCT", 0.15)
        self.exit_before_end = _env_int("AUDIT_EXIT_BEFORE_END", 60)
        self.monitor_interval = _env_int("AUDIT_MONITOR_INTERVAL", 10)
        self.entry_order_timeout = _env_float("AUDIT_ENTRY_ORDER_TIMEOUT", self.entry_order_timeout)
        self.exit_order_timeout = _env_float("AUDIT_EXIT_ORDER_TIMEOUT", self.exit_order_timeout)

    def summary(self):
        wins = sum(1 for t in self.trades if t.get("won"))
        total_pnl = sum(t.get("pnl", 0) for t in self.trades)
        print(f"\n=== LIVE Summary: {wins}/{len(self.trades)} wins | P&L ${total_pnl:+.2f} | Bank ${self.bankroll:.2f} ===")


if __name__ == "__main__":
    import sys
    single = "--single" in sys.argv
    audit = "--audit" in sys.argv
    if audit:
        single = True
    trader = LiveTrader(single=single, audit=audit)
    if audit:
        print("*** AUDIT MODE — single trade with permissive thresholds ***", flush=True)
    elif single:
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
