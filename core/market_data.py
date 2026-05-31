"""
market_data.py -- Global macro cues via yfinance.

Indian instrument prices (indices, equities, F&O, option premiums) are fetched
from INDstocks in core.indstocks_data. This module only provides the global
cues that INDstocks does not expose (Brent crude, USD/INR, DXY).
"""
import yfinance as yf

from config.logger import get_logger

log = get_logger("market_data")


def fetch_global_data() -> dict:
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
    return result
