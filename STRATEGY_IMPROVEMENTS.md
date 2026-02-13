# Strategy & Logic Improvement Ideas

Codex review of the bot's strategy and execution logic. Status tracked below.

## Highest-Impact Fixes (Correctness + Execution)

1. ~~Confirm fills before assuming a position exists~~ **DONE**
   - GTC orders confirmed via polling `get_order()` with configurable timeout (20s default)
   - Unfilled orders auto-cancelled after timeout to prevent stale resting orders

2. ~~Use CLOB prices for entry decisions~~ **DONE**
   - Entry uses `get_order_book()` for live best ask, spread, depth

3. ~~Fix sell minimum-order logic~~ **DONE**
   - Never inflates shares beyond what's owned; skips sell if below $1 min

4. ~~Bankroll and P&L must reflect actual fills~~ **DONE**
   - Records `size_matched` and fill price from order status

## Edge Improvements (Signal + EV)

5. Replace fixed fair values with calibrated probabilities
   - Needs historical data; save for after bot has run enough intervals

6. Normalize move size by volatility (regime filter)
   - Good idea, needs data

7. Add persistence filter
   - Require BTC move to hold for N seconds before entering

8. ~~Liquidity/spread filter~~ **DONE**
   - Skips entry if spread > MAX_SPREAD (default 6 cents)

## Exit Logic Improvements

9. Time-based decay of TP/SL thresholds
   - Shrink TP and tighten SL as resolution approaches

10. Trailing stop after reaching profit
    - Lock in gains dynamically

11. ~~Confirm sell fills and handle partial fills~~ **DONE**
    - Sells use aggressive GTC limit orders at `best_bid - $0.02` floor price
    - Confirms actual exit price via `get_order()` polling; cancels and retries if unfilled after timeout

## Risk & Capital Management

12. Dynamic sizing based on edge
    - Fractional Kelly or capped sizing; needs calibrated probabilities first

13. ~~Session risk limits~~ **DONE**
    - Circuit breaker stops trading after MAX_SESSION_LOSS ($15 default)

14. Skip low-liquidity windows
    - Ties into spread filter (#8); could add time-of-day filtering

## Engineering & Ops Quality

15. Use real ET timezone (DST safe)
    - Replace fixed UTC-5 with `America/New_York` via `zoneinfo`

16. Persist and recover open positions on restart
    - On startup, check for open orders/positions and resume monitoring

17. ~~Record book snapshot at entry~~ **DONE**
    - Logs best bid, best ask, spread, bid depth, ask depth at entry time
