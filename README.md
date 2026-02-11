# Polymarket 15-Min Up/Down Bot

## Setup

1. Copy `.env.example` → `.env` and fill in your keys
2. Install deps: `pip install -r requirements.txt`
3. Discover active markets: `python discover_markets.py`
4. Run data collector: `python collector.py`

## Architecture

```
binance_ws.py      → Real-time BTC/ETH price feed from Binance
polymarket_ws.py   → Order book + trade feed from Polymarket CLOB
discover_markets.py → Find active 15-min Up/Down market IDs
collector.py       → Phase 1: Dual-feed data collection & logging
config.py          → Environment config loader
```

## Phases

- [x] Phase 1: Data collection (Binance + Polymarket WebSockets)
- [ ] Phase 2: Signal generation (mispricing detection)
- [ ] Phase 3: Execution (maker orders via py-clob-client)
- [ ] Phase 4: Deploy (Railway / Digital Ocean)
