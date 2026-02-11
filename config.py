import os
from dotenv import load_dotenv

load_dotenv()

# Polymarket
POLY_API_KEY = os.getenv("POLY_API_KEY")
POLY_API_SECRET = os.getenv("POLY_API_SECRET")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE")
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY")
POLY_FUNDER = os.getenv("POLY_FUNDER")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

# Binance WebSocket
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
BINANCE_STREAMS = {
    "BTC": "btcusdt@trade",
    "ETH": "ethusdt@trade",
}

# Logging
LOG_DIR = "logs"
DATA_DIR = "data"
