# Polymarket Copy-Trade Bot

Copy-trades profitable wallets on Polymarket. Monitors the crypto leaderboard for high-win-rate traders, then mirrors their trades automatically.

## Setup

1. Copy `.env.example` -> `.env` and fill in your keys
2. Install deps: `pip install -r requirements.txt`
3. Find wallets to follow: `python discover_wallets.py`
4. Add target wallet addresses to `COPY_TARGETS` in `.env`
5. Deploy to Railway (or run locally for discovery only)

## Wallet Discovery (local)

Find and evaluate top crypto traders from the Polymarket leaderboard.

```
python discover_wallets.py                              # top 50, default filters
python discover_wallets.py --min-win-rate 0.65          # stricter win rate
python discover_wallets.py --min-positions 20           # more sample size
python discover_wallets.py --time-period ALL            # all-time leaderboard
python discover_wallets.py --detail 0xabc123...         # deep dive one wallet
```

Output includes win rate, W/L record, total PnL, crypto focus %, and open/closed positions.

## Copy-Trade Bot (Railway)

Polls target wallets every few seconds and mirrors their trades.

```
# Env vars (set in .env or Railway dashboard)
COPY_TARGETS=0xwallet1,0xwallet2     # proxy wallet addresses to follow
COPY_SIZE_USD=10.0                   # flat USD per copied trade
COPY_MAX_PRICE=0.95                  # skip near-certain outcomes
COPY_MIN_PRICE=0.05                  # skip ultra-speculative
COPY_POLL_INTERVAL=5                 # seconds between polls
COPY_LOOKBACK_SECONDS=30             # ignore trades older than this
COPY_MAX_OPEN_POSITIONS=10           # max concurrent positions
COPY_MAX_DAILY_SPEND=100.0           # daily USD cap
```

## Architecture

```
Copy-trade bot:
  copy_trader.py       -> Main entry point (polling loop, startup, shutdown)
  copy_config.py       -> Copy-trade env var loading (extends config.py)
  trade_mirror.py      -> Trade detection (TradeMonitor) + order execution (TradeMirror)

Wallet discovery:
  discover_wallets.py  -> CLI for leaderboard scanning + wallet ranking
  wallet_analyzer.py   -> Data API fetching + scoring logic

Infrastructure:
  config.py            -> Base env config loader (.env)
  polymarket_ws.py     -> CLOB order book WebSocket feed
  discover_markets.py  -> Find active market IDs via Gamma API
  set_allowances.py    -> On-chain token approvals (USDC + CTF)
  derive_creds.py      -> Derive API credentials (IP-bound)
  check_balance.py     -> Check wallet balance
  check_pk.py          -> Validate private key setup

Testing:
  e2e_test.py          -> End-to-end order placement test
  test_auth.py         -> Authentication test
  test_order.py        -> Order placement test
  test_slug.py         -> Market slug discovery test
```

## Deploy

Railway (EU West) -- auto-deploys on push to `main`. Dockerfile runs `copy_trader.py` by default.
