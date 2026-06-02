"""
market_data.py -- Global macro cues via yfinance.

Indian instrument prices (indices, equities, F&O, option premiums) are fetched
from INDstocks in core.indstocks_data. This module only provides the global
cues that INDstocks does not expose (Brent crude, USD/INR, DXY).
"""
import time

import yfinance as yf

from config.logger import get_logger

log = get_logger("market_data")

# Macro cues move slowly; cache so event-driven generation (every ~1-2 min) doesn't
# refetch three yfinance tickers each time.
_GLOBAL_TTL_SEC = 300
_global_cache: tuple[dict, float] | None = None


def fetch_global_data() -> dict:
    global _global_cache
    if _global_cache and (time.time() - _global_cache[1]) < _GLOBAL_TTL_SEC:
        return _global_cache[0]
    result = {}
    tickers = {
        "crude_brent": "BZ=F",
        "usd_inr":     "USDINR=X",
        "dxy":         "DX-Y.NYB",
    }
    for key, symbol in tickers.items():
        try:
            hist = yf.Ticker(symbol).history(period="1d", interval="5m")
            if not hist.empty:
                result[key] = round(float(hist["Close"].iloc[-1]), 4)
        except Exception as e:
            log.warning("%s fetch failed: %s", key, e)
    if result:
        _global_cache = (result, time.time())
    return result
