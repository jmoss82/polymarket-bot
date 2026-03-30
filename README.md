# Polymarket Bot

## Setup

1. Copy `.env.example` → `.env` and fill in your keys
2. Install deps: `pip install -r requirements.txt`
3. Discover active markets: `python discover_markets.py`

## Architecture

```
config.py           → Environment config loader (.env)
discover_markets.py → Find active market IDs via Gamma API
polymarket_ws.py    → Order book + trade feed from Polymarket CLOB
set_allowances.py   → On-chain token approvals (USDC + CTF)
derive_creds.py     → Derive API credentials (IP-bound)
check_balance.py    → Check wallet balance
check_pk.py         → Validate private key setup
```

## Testing

```
e2e_test.py    → End-to-end order placement test
test_auth.py   → Authentication test
test_order.py  → Order placement test
test_slug.py   → Market slug discovery test
```

## Deploy

Railway (EU West) — auto-deploys on push to `main`.
