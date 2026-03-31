"""
Configuration for the copy-trade bot.
Extends config.py with copy-trade-specific env vars.
"""
import os
from config import *  # noqa: F401,F403 — re-export base config

# Target wallets (comma-separated proxy wallet addresses)
COPY_TARGETS = [a.strip() for a in os.getenv("COPY_TARGETS", "").split(",") if a.strip()]

# Position sizing
COPY_SIZE_USD = float(os.getenv("COPY_SIZE_USD", "10.0"))
COPY_MAX_PRICE = float(os.getenv("COPY_MAX_PRICE", "0.95"))
COPY_MIN_PRICE = float(os.getenv("COPY_MIN_PRICE", "0.05"))

# Polling
COPY_POLL_INTERVAL = int(os.getenv("COPY_POLL_INTERVAL", "5"))
COPY_LOOKBACK_SECONDS = int(os.getenv("COPY_LOOKBACK_SECONDS", "30"))

# Risk limits
COPY_MAX_OPEN_POSITIONS = int(os.getenv("COPY_MAX_OPEN_POSITIONS", "10"))
COPY_MAX_DAILY_SPEND = float(os.getenv("COPY_MAX_DAILY_SPEND", "100.0"))
