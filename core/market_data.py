"""
market_data.py -- Fetch market data for configured instruments via INDstocks + global data via yfinance
"""
import yfinance as yf

from config.logger import get_logger
from config.settings import TRACKED_INDICES, TRACKED_STOCKS, TRACKED_COMMODITIES

log = get_logger("market_data")


def fetch_index_data(session) -> dict:
    """Fetch indices, stocks, and commodities via INDstocks."""
    result = {}
    all_ids = TRACKED_INDICES + TRACKED_STOCKS + TRACKED_COMMODITIES

    if not all_ids:
        return result

    ids_str = ",".join(all_ids)

    try:
        resp = session.get("/market/quotes/ltp", params={"security_id": ids_str, "exchange_segment": "NSE"})
        quotes = resp.get("data", [])
        for q in quotes:
            security_id = str(q.get("security_id", ""))
            ltp = float(q.get("last_traded_price", 0))
            name = q.get("trading_symbol", security_id)
            if ltp > 0:
                result[name] = {"ltp": ltp, "security_id": security_id}
    except Exception as e:
        log.warning("Market data fetch failed: %s", e)

    return result


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


def get_all_market_data(session) -> dict:
    data = {**fetch_index_data(session), **fetch_global_data()}
    log.debug("Market data: %s", data)
    return data
