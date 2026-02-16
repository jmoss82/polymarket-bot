# Trading Strategy

## Overview

This bot trades Polymarket's **BTC 15-minute Up/Down** binary markets using real-time **Chainlink** price data as its signal source.

**Core thesis:** If BTC has moved meaningfully in one direction during a 15-minute interval *and* the broader trend supports that direction, the momentum is likely to continue through resolution. The bot uses TEMA(10)/TEMA(80) on 5-minute candles to determine trend, detects intra-interval moves via Chainlink (the same oracle Polymarket uses for resolution), and only enters when both agree.

The strategy is a trend-aligned momentum bet with edge filtering — it only enters when the signal direction matches the TEMA trend and the predicted fair value exceeds the market's current price by a meaningful margin.

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

### TEMA Trend Filter

Before evaluating any intra-interval signals, the bot determines the broader trend using TEMA (Triple Exponential Moving Average) on 5-minute BTC candles:

- **TEMA(10)** — fast line, tracks ~50 minutes of price action
- **TEMA(80)** — slow line, tracks ~400 minutes (~6.7 hours) of price action
- **Trend = "Up"** when TEMA(10) > TEMA(80)
- **Trend = "Down"** when TEMA(10) < TEMA(80)
- **Trend = "Neutral"** when insufficient data (no filtering applied)

On startup, historical candles are bootstrapped from Binance US REST API (259 candles). TEMA updates live as each 5-minute candle closes from Chainlink price ticks.

**Key behavior**: TEMA can update mid-interval (at each 5-min candle close), allowing the trend to flip within a single 15-minute segment. This was observed in testing — trend flipped from Up to Down mid-segment and correctly allowed a Down entry.

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

### Direction + Trend Alignment

Simple momentum — trade in the direction of the move, **but only if the trend agrees**:

- BTC price **above** open → signal is **Up**
- BTC price **below** open → signal is **Down**
- **Signal must match TEMA trend direction** — if signal is "Down" but trend is "Up", the entry is blocked and logged as `[FILTERED]`
- If trend is "Neutral", no filtering is applied (signal passes through)

### Outcome Validation

The bot reads the `outcomes` field from the Gamma API (e.g., `["Up", "Down"]`) and builds a lookup map to determine which `clobTokenId` index corresponds to which outcome. The ordering is never assumed.

### Edge Calculation

The bot estimates a "fair value" for the probability of the predicted outcome, then compares it to the **actual buy price** (what it would cost to fill):

```
buy_price = min(best_ask + 0.01, 0.99)
edge = round(fair_value - buy_price, 4)
```

Edge is rounded to 4 decimal places to avoid IEEE 754 floating-point precision issues (e.g., `0.70 - 0.68` producing `0.019999...` instead of `0.02`).

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
- Prefers `average_matched_price` over the limit `price` when available — uses explicit None/zero checks (Python `or` chains treat `0` as falsy, masking valid values)
- If fill quantity arrives before the average price, does one extra re-poll to let the API propagate the execution price
- Logs raw `avg`/`match`/`limit` price fields for diagnostics
- If `MATCHED` with `size_matched=0`: waits at least **5 poll cycles** before accepting (guards against phantom fills)
- If status is "CANCELED"/"EXPIRED": order didn't fill, position skipped
- **Timeout cancellation**: if not filled within the timeout, the bot calls `cancel(order_id)` to prevent stale orders resting on the book

---

## Exit Logic (Auto-Sell)

After entering a position, the bot uses two exit mechanisms: a resting target sell and a time-based forced exit.

### Target Price Sell (Primary Exit)

Immediately after a buy fills, the bot places a **resting GTC SELL** at a fixed target price (default $0.88). This sits on the book and fills automatically if the share price reaches the target at any point during the interval.

- **Non-blocking placement**: The target sell is placed by the monitor loop, not inline after the buy. This avoids blocking the Chainlink price feed during on-chain settlement delays.
- **Settlement grace period**: Waits 5 seconds after buy fill before first attempt (tokens must settle on-chain).
- **Automatic retries**: If placement fails due to settlement lag ("not enough balance / allowance"), the monitor loop retries every 2 seconds indefinitely until it succeeds or the interval ends.
- **Allowance refresh**: Calls `update_balance_allowance(CONDITIONAL, token_id)` before each placement attempt.

### Forced Exit (Safety Net)

If the target sell hasn't filled with **30 seconds remaining** before resolution:

1. Cancel the resting target sell (if active)
2. Place an aggressive market sell at `best_bid - $0.02`
3. This is pure damage control — if we haven't hit our target by now, get out before resolution

### Sell Mechanics

- Share size is **truncated** to 2 decimal places (`math.floor(shares * 100) / 100`) to prevent selling fractionally more than owned
- All forced sells fetch the **actual CLOB bid price** (`get_price(token_id, "SELL")`) for floor pricing
- Places an **aggressive GTC limit SELL** with a floor of `best_bid - $0.02`
- Polls `get_order()` for up to **20 seconds** (`EXIT_ORDER_TIMEOUT`) to confirm fill
- **Max 3 sell attempts** — if all fail, marks position as exited to prevent spam

### Why This Strategy

- **No percentage-based TP/SL**: Old approach capped upside (TP at 25%) or exited on noise (SL at 25%). Share price swings early in an interval don't mean much — what matters is final direction.
- **Fixed target at $0.88**: Captures meaningful profit without holding to the volatile final seconds. Entry at ~$0.70 means ~25% return.
- **Time-based SL at 30s**: If we haven't hit $0.88 by the last 30 seconds, we were likely wrong. Exit to limit damage rather than risk binary resolution.

### Resolution After Early Exit

When the interval ends:

- If **already sold** (target or forced): uses the sell P&L and logs what would've happened if held
- If **held to resolution**: uses binary win/lose logic (shouldn't happen with current config)

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

1. **Signal logic triggers opposite to trend**: Current move-from-open signal often fires in the wrong direction (noise), then gets filtered by TEMA. Many intervals produce no entry because the signal never fires in the trend direction.
2. **Static fair values**: The 0.70/0.75/0.85 estimates are guesses, not calibrated from data
3. **Single trade per interval**: No re-entry after early exit
4. **REST polling for monitoring**: CLOB prices checked every 2s via REST, not WebSocket
5. **Target price is static**: $0.88 may not be optimal for all market conditions — may need dynamic adjustment
6. **No TEMA dead zone**: TEMAs 50 cents apart trigger same confidence as TEMAs $500 apart. Mid-crossover entries have low conviction.
7. **5-min TEMA lags real-time shifts**: Can show "Up" when 1-min chart has clearly turned down
8. **Settlement lag is unpredictable**: Target sell placement depends on on-chain settlement speed, which varies

---

## Planned Improvements

- **Redesign entry logic**: Use TEMA direction proactively — look for discount entries (dips in uptrend) instead of waiting for confirmation
- **Dynamic target price**: Adjust target based on entry price, time remaining, or volatility instead of fixed $0.88
- **Reach ratio**: Measure probability of BTC reaching the Price to Beat given ATR and time remaining
- **Volume-based indicators**: CVD, RVOL, or VWAP for trade conviction
- **Multi-timeframe TEMA**: 1-min TEMA for entry timing, 5-min for directional filter
- **TEMA dead zone**: Minimum gap threshold before declaring trend (avoid low-confidence crossover entries)
- **WebSocket monitoring**: Stream CLOB orderbook updates for faster exit detection
- **Position recovery on restart**: Check for open positions and resume monitoring

---

## Configuration Reference

All parameters can be overridden via environment variables in Railway.

### General

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| Price feed | `PRICE_FEED` | chainlink | Price source: `chainlink` (resolution-matched) or `binance` (fallback) |

### Trend Filter

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| Candle interval | `TREND_CANDLE_INTERVAL` | 300 (5 min) | Candle size in seconds for TEMA calculation |
| TEMA fast period | `TREND_FAST_PERIOD` | 10 | Fast TEMA period (10 x 5min = 50 min lookback) |
| TEMA slow period | `TREND_SLOW_PERIOD` | 80 | Slow TEMA period (80 x 5min = 6.7 hr lookback) |

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
| Target price | `EXIT_TARGET_PRICE` | 0.88 | Resting GTC sell price (primary exit) |
| Forced exit | `EXIT_BEFORE_END` | 30s | Force sell with this many seconds remaining |
| Monitor interval | `MONITOR_INTERVAL` | 2s | How often to check position and try target sell placement |
| Entry order timeout | `ENTRY_ORDER_TIMEOUT` | 20s | Seconds to wait for buy fill before cancelling |
| Exit order timeout | `EXIT_ORDER_TIMEOUT` | 20s | Seconds to wait for sell fill before cancelling |

### Risk Parameters

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| Max session loss | `MAX_SESSION_LOSS` | $15.00 | Stop trading after this cumulative loss |
