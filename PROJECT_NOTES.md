# Polymarket Bot — Project Notes

## Repo
- GitHub: jmoss82/polymarket-bot
- Local clone: C:\Users\Clawdbot\Desktop\Polymarket Bot\polymarket-bot

## Railway
- Project ID: 3ba135cd-eefe-48ee-bfc7-1a5684091b37
- Region: EU West (Netherlands)
- Auto-deploys on each commit to `main`

## Current Focus
- **TEMA trend filter** — TEMA(10)/TEMA(80) on 5-min candles, bootstrapped from Binance US, updated live from Chainlink. Only enters trades where signal direction matches trend direction.
- **Chainlink price feed** via Polymarket RTDS — same oracle used for market resolution
- **Full entry/exit pipeline** — GTC limit orders, pre-sell allowance refresh, fill confirmation, TP/SL/forced exit
- Buy pricing: `best_ask + $0.01` (aggressive GTC limit to fill quickly)
- Sell pricing: `best_bid - $0.02` floor (aggressive GTC limit sell)
- Edge calculated against actual buy price (`best_ask + 0.01`), not bare `best_ask`
- Outcome ordering validated from Gamma API `outcomes` field (never assumed)
- Position monitoring via CLOB `get_midpoint()` every 5s
- Circuit breaker: stops trading after $15 session loss

## Wallet Setup (MetaMask + Gnosis Safe)
- EOA (signer): 0x8D274d837cAB3E3BE35Cfc03A92aC9Ad5fd87192
- Proxy wallet (funder): 0x213328d670F8ce51F3F3F8bf1208E6672314107B
- POLY_FUNDER must be the proxy wallet address
- signature_type must be 2 (POLY_GNOSIS_SAFE), not 0 (EOA)
- API creds are derived via derive_api_key() on each deploy (IP-bound)
- Conditional token allowances: set via Polymarket UI "Enable Trading", then `update_balance_allowance()` refreshes CLOB cache on startup and before each sell

## Key Files
- **live_trader.py**: live trading logic, TEMA trend filter, exit management, CLOB orderbook integration
- **trend.py**: TEMA(10/80) calculation on 5-min candles, Binance bootstrap, live candle updates
- **entry_observer.py**: paper-mode tester — same signal + TEMA logic as live, with running W/L scoreboard
- **chainlink_ws.py**: Chainlink price feed via Polymarket RTDS WebSocket (resolution-matched)
- **binance_ws.py**: Binance price feed (fallback, set `PRICE_FEED=binance`)
- **set_allowances.py**: standalone script for on-chain token approvals (USDC + CTF)
- **e2e_test.py**: minimal 6-step end-to-end order test (diagnostic tool)
- **config.py**: environment variable loading (.env + Railway)

## Notes
- US-based access: run live tests only on Railway (EU West)
- MetaMask EOA has native USDC (~$50) — Polymarket does NOT use this token
- Proxy wallet has USDC.e (~$60) — this is what Polymarket uses
- Gamma API `outcomePrices` is stale/cached — never use for pricing, only market discovery
- Polymarket resolves BTC 15-min markets using **Chainlink Data Streams** (aggregated from multiple CEX sources)
- `py-clob-client` v0.34.5 has NO `set_allowances()` method — use `update_balance_allowance()` to refresh CLOB cache

## Issues Found & Fixed (2026-02-12)
1. POLY_FUNDER was set to the EOA, not the proxy wallet → balance showed $0
2. signature_type was 0 (EOA) instead of 2 (Gnosis Safe) → "invalid signature"
3. Builder API creds don't work with the CLOB API → 401 Unauthorized
4. Gamma API prices stale → SL didn't fire until -60% → switched to CLOB orderbook
5. Buy orders not filling → limit at stale price sat on book → now uses CLOB best ask
6. Sell failed: "not enough balance/allowance" → unfilled buy → added fill confirmation + cancel
7. Sell retry spam → capped at 3 attempts
8. Fill timeout too short (5s) → order filled after bot gave up → increased to 15s + cancel on timeout
9. NoneType crash → status line accessed trade data when no position existed → added guard

## Issues Found & Fixed (2026-02-13)
10. GTC orders sitting "LIVE" 15s without filling — initially switched to FOK market orders.
11. `price_up`/`price_down` undefined crash — every successful fill crashed in `_evaluate` before recording `iv.trade`, so positions were never tracked, monitored, or auto-exited.
12. GTC partial fills — one position filled in 3 incremental chunks (3.70, 3.00, 0.14 shares).
13. FOK orders rejected → switched to FAK → FAK also rejected → **reverted to GTC with aggressive pricing** (`best_ask + $0.01` buys, `best_bid - $0.02` sells) plus timeout + auto-cancel. Most reliable path through the matching engine.
14. Sells failing with `not enough balance / allowance` — root cause: `set_allowances()` doesn't exist in py-clob-client v0.34.5. Fixed by using `update_balance_allowance(AssetType.CONDITIONAL)` to refresh the CLOB server's allowance cache at startup and before each sell.
15. MATCHED fallback in `_confirm_fill` created phantom fills — `MATCHED` with `size_matched=0` was immediately trusted as a full fill. Now requires 5 poll cycles before accepting.
16. Exit P&L used `trade["shares"]` instead of actual `filled_size` — partial exit fills overcounted bankroll. Fixed to use `min(filled_size, trade["shares"])`.
17. TP/SL sells used midpoint for floor price instead of actual bid — less aggressive than forced exits. Fixed: all sells now fetch the actual CLOB bid price.
18. Forced exit triggered on every Binance tick before the monitor throttle — could spam sell attempts. (Structural issue, mitigated by sell_attempts cap.)

## Issues Found & Fixed (2026-02-14)
19. **Price source mismatch** — bot signaled on Binance BTC/USDT but Polymarket resolves on Chainlink Data Streams. Created `chainlink_ws.py` using Polymarket's RTDS WebSocket (`crypto_prices_chainlink` topic). Chainlink is now default; Binance available via `PRICE_FEED=binance`.
20. **Token ID ordering assumed, never validated** — `clobTokenIds[0]` was assumed to be "Up". Now reads the `outcomes` field from Gamma API and builds a lookup map.
21. **Edge calculated on `best_ask`, order placed at `best_ask + 0.01`** — edge was 1 cent better than reality. Now edge is calculated against the actual buy price.
22. **`last_signal_minute` served two conflicting purposes** — below-threshold logging and evaluation throttle used the same variable with overlapping ranges, defeating the 30s throttle. Split into `last_log_minute` and `last_eval_half_min`.
23. **Fill price was limit price, not execution price** — `_confirm_fill` used Python `or` chain for price fallback, but `0` is falsy so `average_matched_price: 0` silently fell through to the limit price. Fixed with explicit None/zero checks, raw field logging (`avg=X match=Y limit=Z`), and a re-poll if fill quantity arrives before the average price.
24. **Transient order failures locked out entire interval** — `trade_taken = True` was set before order submission. Now only set after the CLOB accepts the order, allowing retry on API errors.
25. **Take profit too aggressive at 50%** — first successful trade peaked at +46% P&L, sat open for 7+ minutes without exiting. Lowered TP to 25% (get in and out faster, reduce exposure).
26. **Floating-point edge comparison** — `0.70 - 0.68` = `0.01999...` in IEEE 754, causing trades at exactly MIN_EDGE to be rejected. Fixed with `round(edge, 4)`.
27. **Startup allowance refresh failed for Conditional Tokens** — ERC1155 requires a valid `token_id` which we don't have at startup. Removed startup refresh for CONDITIONAL; real refresh happens before each sell with the actual token_id.

## Changes (2026-02-15)
28. **TEMA trend filter added** — new `trend.py` module calculates TEMA(10) and TEMA(80) on 5-min candles. Historical candles bootstrapped from Binance US REST API on startup; updates live as each 5-min candle closes from Chainlink ticks. Signals that conflict with trend direction are blocked (`[FILTERED]`). When trend is "Neutral" (insufficient data or TEMAs equal), no filtering applied.
29. **Entry observer upgraded** — `entry_observer.py` now includes TEMA filter and running W/L scoreboard. Supports `--no-filter` flag for baseline comparison. Paper testing showed ~50% win rate without trend filter, improved with filter active.
30. **First successful live round-trip with TEMA** — Entered STRONG Up at $0.71 (trend=Up), position peaked at +16.2%, forced exit at 60s sold at $0.79 for $0.56 profit. Would have been $2.03 if held to resolution (confirmed win). Exit mechanics (TP/SL/forced) now fully functional.

## Next Steps
- Observe live trades with TEMA filter and current exit config (TP 25% / SL 25% / forced 60s)
- Evaluate whether forced exit at 60s is leaving money on the table (first live trade: $0.56 vs $2.03 if held)
- Redesign entry logic to use trend direction proactively (enter on discount, not confirmation)
- Add reach ratio / volatility-based entry filtering (ATR + distance to PTB)
- Volume-based indicators for conviction (CVD, RVOL, or VWAP)
- Consider multi-timeframe TEMA (1-min for timing, 5-min for direction)
- Dead zone for TEMA crossovers (minimum gap threshold before declaring trend)
