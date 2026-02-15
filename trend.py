"""
Trend detection via TEMA (Triple Exponential Moving Average).

Calculates TEMA on BTC candles to determine short-term and longer-term trend.
Bootstraps historical candles from Binance REST API on startup, then updates
live from Chainlink price feed as each candle closes.

Usage:
    trend = TrendTracker(candle_interval=300, fast_period=10, slow_period=80)
    await trend.bootstrap()             # fetch history from Binance
    trend.update_price(price, ts)       # call on every Chainlink tick
    signal = trend.get_trend()          # "Up", "Down", or "Neutral"
"""
import os
import time
import aiohttp
from collections import deque

# ── Config ──────────────────────────────────────────────────
BINANCE_KLINES_URLS = [
    "https://api.binance.us/api/v3/klines",   # US-accessible
    "https://api.binance.com/api/v3/klines",   # Fallback (may 451 from US)
]

# Candle interval in seconds (default 300 = 5 minutes)
CANDLE_INTERVAL = int(os.getenv("TREND_CANDLE_INTERVAL", "300"))

# TEMA periods
FAST_PERIOD = int(os.getenv("TREND_FAST_PERIOD", "10"))
SLOW_PERIOD = int(os.getenv("TREND_SLOW_PERIOD", "80"))

# Map candle interval (seconds) to Binance interval string
_BINANCE_INTERVALS = {
    60: "1m",
    180: "3m",
    300: "5m",
    900: "15m",
    1800: "30m",
    3600: "1h",
}


def _calc_ema(values, period):
    """Calculate EMA over a list of values. Returns list of same length (NaN-padded)."""
    if len(values) < period:
        return [None] * len(values)

    k = 2.0 / (period + 1)
    ema = [None] * (period - 1)
    # Seed with SMA of first `period` values
    ema.append(sum(values[:period]) / period)

    for i in range(period, len(values)):
        ema.append(values[i] * k + ema[-1] * (1 - k))

    return ema


def calc_tema(closes, period):
    """Calculate TEMA for a list of close prices.
    Returns the most recent TEMA value, or None if not enough data.

    TEMA = 3*EMA1 - 3*EMA2 + EMA3
    where EMA1 = EMA(close), EMA2 = EMA(EMA1), EMA3 = EMA(EMA2)
    """
    if len(closes) < period * 3:
        return None

    ema1 = _calc_ema(closes, period)

    # EMA2 = EMA of EMA1 (skip None values)
    ema1_clean = [v for v in ema1 if v is not None]
    ema2 = _calc_ema(ema1_clean, period)

    # EMA3 = EMA of EMA2
    ema2_clean = [v for v in ema2 if v is not None]
    ema3 = _calc_ema(ema2_clean, period)

    if not ema3 or ema3[-1] is None:
        return None

    # Get the last values of each EMA level
    e1 = ema1[-1]
    e2 = ema2[-1]
    e3 = ema3[-1]

    if e1 is None or e2 is None or e3 is None:
        return None

    return 3 * e1 - 3 * e2 + e3


class TrendTracker:
    def __init__(self, candle_interval=None, fast_period=None, slow_period=None):
        self.candle_interval = candle_interval or CANDLE_INTERVAL
        self.fast_period = fast_period or FAST_PERIOD
        self.slow_period = slow_period or SLOW_PERIOD

        # We need at least slow_period * 3 candles for TEMA to stabilize
        self._min_candles = self.slow_period * 3 + 10
        self._closes = deque(maxlen=self._min_candles + 50)

        # Current candle tracking
        self._current_candle_start = 0
        self._current_candle_close = None

        # Cached TEMA values
        self.tema_fast = None
        self.tema_slow = None
        self._ready = False

    def _get_candle_start(self, ts_seconds=None):
        """Get the start timestamp of the candle containing the given time."""
        ts = ts_seconds or time.time()
        return int(ts // self.candle_interval) * self.candle_interval

    async def bootstrap(self):
        """Fetch historical candles from Binance to seed TEMA calculation."""
        interval_str = _BINANCE_INTERVALS.get(self.candle_interval, "5m")
        num_candles = self._min_candles + 10  # extra buffer
        limit = min(num_candles, 1000)

        print(f"[Trend] Fetching {limit} x {interval_str} candles from Binance...", flush=True)

        for base_url in BINANCE_KLINES_URLS:
            url = f"{base_url}?symbol=BTCUSDT&interval={interval_str}&limit={limit}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            print(f"[Trend] {base_url} returned {resp.status}, trying next...", flush=True)
                            continue

                        data = await resp.json()

                        if not data or len(data) < self.slow_period:
                            print(f"[Trend] Not enough candles: got {len(data)}, need {self.slow_period}", flush=True)
                            return False

                        # Kline format: [open_time, open, high, low, close, volume, close_time, ...]
                        # Use close price (index 4) — skip the last candle (still forming)
                        for candle in data[:-1]:
                            close = float(candle[4])
                            self._closes.append(close)

                        # Set current candle start from the last (still-forming) candle
                        self._current_candle_start = int(data[-1][0]) // 1000
                        self._current_candle_close = float(data[-1][4])

                        self._recalc()

                        status = "READY" if self._ready else "WARMING UP"
                        print(f"[Trend] Loaded {len(self._closes)} candles | {status}", flush=True)
                        if self._ready:
                            trend = self.get_trend()
                            print(f"[Trend] TEMA({self.fast_period})={self.tema_fast:.2f} | "
                                  f"TEMA({self.slow_period})={self.tema_slow:.2f} | "
                                  f"Trend: {trend}", flush=True)
                        return True

            except Exception as e:
                print(f"[Trend] {base_url} failed: {e}", flush=True)
                continue

        print(f"[Trend] All Binance endpoints failed", flush=True)
        return False

    def update_price(self, price, ts_ms=None):
        """Call on every Chainlink price tick. Manages candle boundaries and
        recalculates TEMA when a candle closes."""
        ts_sec = (ts_ms / 1000.0) if ts_ms else time.time()
        candle_start = self._get_candle_start(ts_sec)

        if self._current_candle_start == 0:
            # First tick — initialize
            self._current_candle_start = candle_start
            self._current_candle_close = price
            return

        if candle_start > self._current_candle_start:
            # New candle started — close the previous one
            if self._current_candle_close is not None:
                self._closes.append(self._current_candle_close)
                self._recalc()

            self._current_candle_start = candle_start
            self._current_candle_close = price
        else:
            # Same candle — update the running close
            self._current_candle_close = price

    def _recalc(self):
        """Recalculate TEMA values from closes."""
        closes_list = list(self._closes)
        self.tema_fast = calc_tema(closes_list, self.fast_period)
        self.tema_slow = calc_tema(closes_list, self.slow_period)
        self._ready = (self.tema_fast is not None and self.tema_slow is not None)

    def get_trend(self):
        """Returns the current trend direction.

        "Up"      — TEMA(fast) > TEMA(slow), short-term trend is bullish
        "Down"    — TEMA(fast) < TEMA(slow), short-term trend is bearish
        "Neutral" — not enough data or TEMAs are equal
        """
        if not self._ready or self.tema_fast is None or self.tema_slow is None:
            return "Neutral"

        if self.tema_fast > self.tema_slow:
            return "Up"
        elif self.tema_fast < self.tema_slow:
            return "Down"
        return "Neutral"

    def get_detail(self):
        """Returns a dict with current trend details for logging."""
        trend = self.get_trend()
        gap = (self.tema_fast - self.tema_slow) if self._ready else 0
        gap_pct = (gap / self.tema_slow * 100) if self._ready and self.tema_slow else 0
        return {
            "trend": trend,
            "tema_fast": round(self.tema_fast, 2) if self.tema_fast else None,
            "tema_slow": round(self.tema_slow, 2) if self.tema_slow else None,
            "gap": round(gap, 2),
            "gap_pct": round(gap_pct, 4),
            "candles": len(self._closes),
            "ready": self._ready,
        }


# ── Standalone test ─────────────────────────────────────────
if __name__ == "__main__":
    import asyncio

    async def main():
        tracker = TrendTracker()
        ok = await tracker.bootstrap()
        if ok:
            detail = tracker.get_detail()
            print(f"\nTrend: {detail['trend']}")
            print(f"TEMA({tracker.fast_period}): ${detail['tema_fast']:,.2f}")
            print(f"TEMA({tracker.slow_period}): ${detail['tema_slow']:,.2f}")
            print(f"Gap: ${detail['gap']:,.2f} ({detail['gap_pct']:+.4f}%)")
            print(f"Candles loaded: {detail['candles']}")

    asyncio.run(main())
