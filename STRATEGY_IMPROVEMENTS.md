# Strategy Improvements

Current state of the bot and prioritized improvements. Last updated: 2026-02-16.

---

## Where We Are Now

The bot trades Polymarket BTC 15-minute Up/Down markets using a **calibrated fair value
table** derived from 90 days of Coinbase BTC-USD data (8,637 intervals, 110,149 observations).

On each price tick, it looks up the win probability for the current (move_size, elapsed_time)
pair, compares it to the market's asking price, and enters if the edge exceeds 2 cents.
Exit is a resting GTC sell at $0.95, with a forced exit at T-30 seconds.

**What's working:**
- Calibrated fair values replace the old static guesses (0.70/0.75/0.85) — the table
  captures the full (move, time) surface, from 59.8% for tiny early moves to 98.2% for
  large late moves
- Lower move threshold (0.03%) catches opportunities the old bot missed — the 0.03-0.05%
  bucket has 80% win rate late in the interval
- Higher max entry price ($0.95) allows late-interval high-confidence entries
- Target sell at $0.95 lets winners run further instead of capping at $0.88
- Infrastructure is solid — Chainlink feed, CLOB orderbook pricing, GTC order management,
  fill confirmation, partial-fill accounting, circuit breaker
- Paper test validated: 3W/1L (75%) across 4 intervals

**What's not working / needs improvement:**
- No per-trade risk management — losers are held until forced exit at T-30s
- Fixed bet size doesn't scale with conviction
- Target price is static — doesn't adapt to entry price or fair value
- Calibration uses Coinbase as proxy for Chainlink (resolution oracle) — ~$10 basis risk

---

## Priority 1: Per-Trade Stop Loss

**Problem:** If you enter at $0.70 and the market drops to $0.40, you hold all the way to
the forced exit at T-30 seconds, crystallizing a large loss. The circuit breaker ($15 session
loss) is a global kill switch, not a per-trade risk tool. With the wider entry price range
(up to $0.95), protecting against downside per trade matters more than before.

**Approach:**
- Add a per-trade stop loss: exit if mark price drops below `entry_price - STOP_OFFSET`
- Suggested starting point: $0.15 offset (e.g., enter at $0.70, stop at $0.55)
- Stop triggers an immediate aggressive sell (same mechanics as forced exit)
- Must use CLOB bid price for stop evaluation, not midpoint
- Don't make the stop too tight — normal noise will trigger it; backtest threshold with data

---

## Priority 2: Dynamic Position Sizing

**Problem:** Fixed bet size doesn't scale with conviction. A 98% win rate entry with 10
cents of edge gets the same size as a 60% entry with 2 cents of edge.

**Approach:**
- Use edge magnitude to scale bet size: higher edge = larger position
- Could use fractional Kelly: `f = edge / (payout - 1)`, capped at some max
- The calibrated fair value table makes this possible — we have real win rates now

---

## Priority 3: Dynamic Target Price

**Problem:** A static $0.95 target means:
- If you buy at $0.55 early, you're targeting +$0.40 — great
- If you buy at $0.90 late, you're targeting +$0.05 — thin
- Earlier entries with strong trends could target higher; late entries should take
  profit sooner

**Approach:**
- Adjust target based on entry price and/or fair value
- Simple rule: `target = max(entry_price + MIN_PROFIT, fair_value + small_buffer)`
- Or time-decay: lower the target as resolution approaches

---

## Priority 4: Recalibrate With Chainlink Data

**Problem:** The fair value table was calibrated from Coinbase BTC-USD data. Polymarket
resolves on Chainlink Data Streams, which is a composite oracle. The ~$10 offset means
~0.01% basis risk — acceptable but not zero.

**Approach:**
- Add a lightweight Chainlink tick logger to save every price update to a file
- After a few weeks, recalibrate the table against actual resolution-oracle data
- Cross-check Coinbase vs Chainlink win rates to quantify the basis gap

---

## Future Considerations (Not Immediate)

- **WebSocket monitoring**: Stream CLOB orderbook updates instead of polling every 2s.
  Would give faster stop-loss and exit detection.
- **Position recovery on restart**: Check for open positions on startup and resume
  monitoring. Currently any restart during a position orphans it.
- **Volume-based indicators**: CVD, RVOL, or VWAP as additional conviction signals.
  Need a reliable real-time volume feed first.
- **Time-of-day filtering**: Some hours may have better signal quality than others.
  Analyze trade logs for patterns.
- **Volatility normalization**: Express moves as ATR multiples instead of raw percentages
  to adapt to changing market regimes.

---

## Completed

- [x] Calibrate fair values from data (calibrate.py, 90-day Coinbase backtest, 4x4 table)
- [x] Remove TEMA as entry filter (zero predictive lift confirmed by calibration)
- [x] Lower move threshold to 0.03% (0.03-0.05% bucket has 80% win rate late)
- [x] Raise max entry price to $0.95 (allows high-confidence late entries)
- [x] Raise target sell to $0.95 (from $0.88, lets winners run)
- [x] Align entry window to 60-840s (matches calibration data boundaries)
- [x] Confirm fills before assuming a position exists
- [x] Use CLOB prices for entry decisions
- [x] Fix sell minimum-order logic
- [x] Bankroll and P&L reflect actual fills
- [x] Liquidity/spread filter
- [x] Confirm sell fills and handle partial fills
- [x] Session risk limits / circuit breaker
- [x] Record book snapshot at entry
- [x] Exit strategy overhaul (resting target sell + forced exit)
- [x] Non-blocking target sell placement
- [x] Chainlink price feed (resolution-matched oracle)
- [x] Edge calculated against actual buy price
