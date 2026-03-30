# Polymarket Bot — Project Notes

## Repo
- GitHub: jmoss82/polymarket-bot
- Local clone: C:\Users\jmoss\OneDrive\Desktop\polymarket-bot

## Railway
- Project ID: 3ba135cd-eefe-48ee-bfc7-1a5684091b37
- Region: EU West (Netherlands)
- Auto-deploys on each commit to `main`
- Railway buffers stdout — always use `flush=True` on print statements

## Wallet Setup (MetaMask + Gnosis Safe)
- EOA (signer): 0x8D274d837cAB3E3BE35Cfc03A92aC9Ad5fd87192
- Proxy wallet (funder): 0x213328d670F8ce51F3F3F8bf1208E6672314107B
- POLY_FUNDER must be the proxy wallet address
- signature_type must be 2 (POLY_GNOSIS_SAFE), not 0 (EOA)
- API creds are derived via derive_api_key() on each deploy (IP-bound)
- Conditional token allowances: set via Polymarket UI "Enable Trading", then `update_balance_allowance()` refreshes CLOB cache on startup and before each sell

## Polymarket CLOB Gotchas
- **py-clob-client v0.34.5** has NO `set_allowances()` method — use `update_balance_allowance()` to refresh CLOB cache
- **Order types**: FOK rejected, FAK rejected — **GTC with aggressive pricing** is the most reliable path (`best_ask + $0.01` buys, `best_bid - $0.02` sells) plus timeout + auto-cancel
- **Fill confirmation**: `MATCHED` with `size_matched=0` should never be trusted as filled. Require nonzero matched quantity. Polymarket sometimes returns `size_matched=7.999997` for an 8-share order (floating-point) — use epsilon comparison (`>= size - 0.001`)
- **Partial fills**: GTC orders can fill in multiple chunks — track `open_shares` and book P&L incrementally
- **Fill price fallback**: `average_matched_price: 0` is falsy in Python — use explicit None/zero checks, not `or` chains
- **Fee-adjusted balances**: Polymarket deducts fees from acquired shares (e.g., bought 8.30, received 8.17). Always query actual balance via `get_balance_allowance(CONDITIONAL, token_id)` before selling
- **Settlement delay**: token balance arrives in stages on Polygon (e.g., 0 → 2 → 7 shares). Don't lock in a low balance — allow upward corrections
- **5-share minimum**: CLOB rejects sell orders below 5 shares with a 400 error
- **$1 minimum order value**: orders below $1 are rejected
- **Dust threshold**: truncation + fees leave unsellable fractional shares (~0.01). Treat as "position closed"
- **Sell size**: use `math.floor(shares * 100) / 100` not `round()` to avoid selling more than owned
- **Allowance refresh**: ERC1155 conditional tokens require a valid `token_id` — can't refresh at startup, do it before each sell
- **Gamma API `outcomePrices`** is stale/cached — never use for pricing, only for market discovery
- **Token ID ordering**: never assume `clobTokenIds[0]` is a specific outcome — read `outcomes` field and build a lookup map
- **Concurrent sells**: guard with a `_sell_in_progress` flag; asyncio cooperative scheduling means set-before-await is atomic
- **`asyncio.get_event_loop()`** is deprecated — use `asyncio.get_running_loop()` (Python 3.10+)

## Environment
- US-based access: run live operations only on Railway (EU West)
- MetaMask EOA has native USDC (~$50) — Polymarket does NOT use this token
- Proxy wallet has USDC.e — this is what Polymarket uses
- Chain ID: 137 (Polygon)
