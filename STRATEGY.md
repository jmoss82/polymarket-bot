# Trading Strategy

## Overview

This bot trades Polymarket's **BTC 15-minute Up/Down** binary markets using real-time **Chainlink** price data as its signal source.

**Core thesis:** If BTC has moved meaningfully in one direction during a 15-minute interval, that momentum is likely to continue through resolution. The bot detects these moves via Chainlink's aggregated price feed (the same oracle Polymarket uses for resolution), then buys the corresponding Up or Down shares on Polymarket before the interval closes.

The strategy is a momentum-based directional bet with edge filtering — it only enters when the predicted fair value exceeds the market's current price by a meaningful margin.

---

## How the Markets Work

Polymarket creates a new BTC Up/Down market every 15 minutes, aligned to UTC boundaries (e.g., 2:00, 2:15, 2:30, 2:45).

- **"Up" shares** pay $1.00 each if BTC's closing price is **>=** its opening price for the interval
- **"Down" shares** pay $1.00 each if BTC closes lower
- Before resolution, shares trade between $0.01 and $0.99 based on market-implied probability
- At resolution: winning shares snap to $1.00, losing shares snap to $0.00
- **Resolution oracle**: Chainlink Data Streams (BTC/USD), aggregated from multiple CEX sources

For example, if "Up" is trading at $0.45, the market implies a 45% chance BTC will close higher than it opened.

---

## Data Sources

| Purpose | Source | Why |
|---------|--------|-----|
| BTC price (signals) | **Chainlink via Polymarket RTDS** | Same oracle used for market resolution |
| Market discovery | **Gamma API** | Slug → token ID mapping, market metadata, outcome ordering |
| Entry pricing | **CLOB orderbook** (`get_order_book`) | Live best bid/ask, spread, depth |
| Position monitoring | **CLOB orderbook** (`get_midpoint`) | Live mid-market price for TP/SL |
| Sell pricing | **CLOB orderbook** (`get_price`) | Live best bid for immediate fills |

Notes:
- The Gamma API's `outcomePrices` field is **cached and stale** (updates every few minutes). It is NOT used for pricing — only for market discovery.
- Binance is available as a fallback feed via `PRICE_FEED=binance` env var.
- The Chainlink feed comes from Polymarket's RTDS WebSocket (`wss://ws-live-data.polymarket.com`, topic `crypto_prices_chainlink`).

---

## Signal Logic

### Interval Tracking

The bot aligns to 15-minute UTC boundaries and tracks BTC prices from Chainlink in real time:

- **Open price**: first price update after the interval starts
- **Latest price**: most recent price update
- **Move %**: `((latest - open) / open) * 100`
- **High/Low**: tracked for diagnostics

### Entry Window

Trades are only considered between **45 and 840 seconds** (0:45–14:00 minutes) into the interval:

- **First 45 seconds skipped**: lets the open price stabilize
- **Last 1 minute skipped**: avoids stale entries too close to resolution

### Signal Strength

| Strength | Condition | When |
|----------|-----------|------|
| **STRONG** | abs(move) >= 0.10% | Any time in entry window |
| **MODERATE** | abs(move) >= 0.05% | Only after 420s (7 minutes) |

Moves below 0.05% are ignored. MODERATE signals require more elapsed time because smaller moves are less reliable early in the interval.

### Direction

Simple momentum — trade in the direction of the move:

- BTC price **above** open → buy **Up**
- BTC price **below** open → buy **Down**

### Outcome Validation

The bot reads the `outcomes` field from the Gamma API (e.g., `["Up", "Down"]`) and builds a lookup map to determine which `clobTokenId` index corresponds to which outcome. The ordering is never assumed.

### Edge Calculation

The bot estimates a "fair value" for the probability of the predicted outcome, then compares it to the **actual buy price** (what it would cost to fill):

```
buy_price = min(best_ask + 0.01, 0.99)
edge = fair_value - buy_price
```

Fair value depends on signal strength and timing:

| Strength | Elapsed > 600s (10 min) | Elapsed <= 600s |
|----------|------------------------|-----------------|
| STRONG | 0.85 | 0.75 |
| MODERATE | 0.70 | 0.70 |

### Entry Filters

All of these must pass before an order is placed:

1. **Edge >= 0.02** — the fair value must exceed the buy price by at least 2 cents
2. **Buy price <= 0.75** — don't buy shares priced above 75 cents (risk/reward too poor)
3. **Spread <= 0.06** — skip if bid/ask spread exceeds 6 cents (liquidity too thin)
4. **Market accepting orders** — Polymarket must have the market open for trading
5. **One trade per interval** — the first accepted order locks out the rest
6. **Circuit breaker off** — session loss must be below the max threshold

### Entry Retry

If the order submission fails (API error, network issue), `trade_taken` is NOT set, allowing the bot to retry on the next evaluation cycle. Once the CLOB accepts the order, no further entries are attempted for that interval.

---

## Order Execution

- Fetches the **CLOB orderbook** to get live best bid, best ask, spread, and depth
- Places a **GTC (Good-Till-Cancelled) limit buy** at `best_ask + $0.01` (aggressive pricing to fill quickly)
- Uses `OrderArgs` with `size` in shares (not dollars) and `create_order()` + `post_order(order, OrderType.GTC)`
- Share size calculated with integer arithmetic to ensure USDC cost (`size * price`) has max 2 decimal places (API requirement)
- Default bet size: **$5 per trade**
- Minimum order value enforced at **$1** (Polymarket's floor)
- Orders are signed with `signature_type=2` (Gnosis Safe) for MetaMask proxy wallet accounts

### Fill Confirmation

After placing a GTC buy, the bot polls for fill status with a configurable timeout:

- Polls `get_order(order_id)` every 1 second for up to **20 seconds** (`ENTRY_ORDER_TIMEOUT`)
- If `size_matched > 0`: records actual fill size and price for accurate P&L
- Prefers `average_matched_price` over the limit `price` when available
- If `MATCHED` with `size_matched=0`: waits at least **5 poll cycles** before accepting (guards against phantom fills)
- If status is "CANCELED"/"EXPIRED": order didn't fill, position skipped
- **Timeout cancellation**: if not filled within the timeout, the bot calls `cancel(order_id)` to prevent stale orders resting on the book

---

## Exit Logic (Auto-Sell)

After entering a position, the bot monitors the live Polymarket price and can sell early.

### Pre-Sell Allowance Refresh

Before every sell attempt, the bot calls `update_balance_allowance(CONDITIONAL, token_id)` to refresh the CLOB server's view of the on-chain conditional token balance. Without this, the CLOB may reject sells with "not enough balance / allowance" because its cache doesn't reflect tokens from a recent buy.

### How It Works

Every **5 seconds** (configurable), the bot fetches the live midpoint price from the CLOB orderbook and compares it to the entry price:

```
position_pnl_pct = (current_price - entry_price) / entry_price
```

Three exit triggers, checked in priority order:

| Trigger | Condition | Default | Behavior |
|---------|-----------|---------|----------|
| **Take Profit** | `pnl_pct >= +50%` | `TAKE_PROFIT_PCT=0.50` | Sell to lock in gains |
| **Stop Loss** | `pnl_pct <= -25%` | `STOP_LOSS_PCT=0.25` | Sell to cut losses |
| **Forced Exit** | `remaining <= 60s` | `EXIT_BEFORE_END=60` | Sell before resolution regardless of P&L |

### Sell Mechanics

- All sells fetch the **actual CLOB bid price** (`get_price(token_id, "SELL")`) for floor pricing
- Places an **aggressive GTC limit SELL** with a floor price of `best_bid - $0.02`
- Uses `OrderArgs` with `size` in shares and `create_order()` + `post_order(order, OrderType.GTC)`
- Polls `get_order()` for up to **20 seconds** (`EXIT_ORDER_TIMEOUT`) to confirm fill
- If not filled within timeout, the order is **cancelled** and the attempt is retried
- P&L uses actual `filled_size` from the sell (not the original entry shares) to handle partial fills correctly
- **Never sells more shares than owned** — if order value < $1 minimum, the sell is skipped
- **Max 3 sell attempts** — if all fail, marks position as exited to prevent spam

### Resolution After Early Exit

When the interval ends:

- If **already sold**: uses the sell P&L (no double-counting) and logs what would've happened if held
- If **held to resolution**: uses the original binary win/lose logic

---

## Risk Management

### Circuit Breaker

The bot tracks cumulative session P&L. If losses exceed the threshold:

- **No new trades** are opened
- Open positions still get monitored and exited normally
- Resets on restart

Default: stop after **$15 cumulative loss** (`MAX_SESSION_LOSS`).

---

## Known Limitations

1. **Momentum-only**: Doesn't consider mean-reversion, volatility regimes, or order flow imbalance
2. **Static fair values**: The 0.70/0.75/0.85 estimates are guesses, not calibrated from data
3. **Single trade per interval**: No re-entry after early exit
4. **REST polling for monitoring**: CLOB prices checked every 5s via REST, not WebSocket (up to 5s blind spot)
5. **GTC fill latency**: Orders may take up to 20s to fill (vs. instant FOK), though aggressive pricing minimizes this

---

## Planned Improvements

- **Data-driven fair values**: Backtest historical intervals to calibrate win rates by move size and timing
- **Volatility normalization**: Scale move thresholds by realized volatility
- **Persistence filter**: Require BTC move to hold for N seconds before entering
- **Trailing stop**: Lock in gains dynamically after hitting a profit threshold
- **Time-decay TP/SL**: Shrink take-profit and tighten stop-loss as resolution approaches
- **Multiple trades per interval**: Re-entry after early exit with cumulative risk limits
- **WebSocket monitoring**: Stream CLOB orderbook updates for sub-second TP/SL reactions
- **Position recovery on restart**: Check for open positions and resume monitoring

---

## Configuration Reference

All parameters can be overridden via environment variables in Railway.

### General

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| Price feed | `PRICE_FEED` | chainlink | Price source: `chainlink` (resolution-matched) or `binance` (fallback) |

### Entry Parameters

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| Bet size | `BET_SIZE` | $5.00 | Dollar amount per trade |
| Min move % | `MIN_MOVE_PCT` | 0.05% | Minimum BTC move to trigger a MODERATE signal |
| Strong move % | `STRONG_MOVE_PCT` | 0.10% | BTC move threshold for a STRONG signal |
| Entry start | `ENTRY_START` | 45s | Earliest second in the interval to enter |
| Entry end | `ENTRY_END` | 840s | Latest second in the interval to enter |
| Min edge | `MIN_EDGE` | 0.02 | Minimum edge (fair - buy price) to take a trade |
| Max entry price | `MAX_ENTRY_PRICE` | 0.75 | Don't buy shares priced above this |
| Max spread | `MAX_SPREAD` | 0.06 | Skip entry if bid/ask spread exceeds this |

### Exit Parameters

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| Take profit % | `TAKE_PROFIT_PCT` | 0.50 (+50%) | Sell when position is up this % from entry |
| Stop loss % | `STOP_LOSS_PCT` | 0.25 (-25%) | Sell when position is down this % from entry |
| Forced exit | `EXIT_BEFORE_END` | 60s | Force sell with this many seconds remaining |
| Monitor interval | `MONITOR_INTERVAL` | 5s | How often to check CLOB prices |
| Entry order timeout | `ENTRY_ORDER_TIMEOUT` | 20s | Seconds to wait for buy fill before cancelling |
| Exit order timeout | `EXIT_ORDER_TIMEOUT` | 20s | Seconds to wait for sell fill before cancelling |

### Risk Parameters

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| Max session loss | `MAX_SESSION_LOSS` | $15.00 | Stop trading after this cumulative loss |
