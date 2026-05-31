"""
advisory_market_data.py — Broker-free market data for the advisory product.

The advisory product does NOT link any broker, so all market data comes from
free public sources (yfinance). This module serves two purposes:

  1. get_market_snapshot() — a compact dict of index / commodity / global levels
     used to ground the AI research engine when it generates calls.

  2. make_price_lookup() — returns a `price_lookup(call) -> float | None`
     callable for core.call_tracker. It maps a call's underlying to a yfinance
     ticker and returns the latest traded price.

Coverage / limitations (best-effort, v1):
  • equity   → NSE cash via "<UNDERLYING>.NS"          (tracked)
  • futures  → index spot or "<UNDERLYING>.NS" proxy    (tracked, basis ignored)
  • index_option → option PREMIUMS have no free feed     (lookup returns None)
  • commodity → MCX prices differ in scale from global
                proxies, so tracking is unreliable        (lookup returns None)

When a price is unavailable the tracker simply leaves the call untouched; only
its expiry can still close it. That is the intended, safe degradation.
"""

import time

import yfinance as yf

from config.logger import get_logger
from core.market_data import fetch_global_data

log = get_logger("advisory_market_data")

# Indian indices → yfinance symbols
INDEX_TICKERS = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "NIFTY 50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "BANK NIFTY": "^NSEBANK",
    "NIFTYBANK": "^NSEBANK",
    "FINNIFTY": "^CNXFIN",
    "INDIAVIX": "^INDIAVIX",
    "VIX": "^INDIAVIX",
}

# Global commodity proxies (COMEX/NYMEX) — for snapshot context only.
COMMODITY_PROXY = {
    "gold": "GC=F",
    "silver": "SI=F",
    "crude_wti": "CL=F",
    "natgas": "NG=F",
}

_PRICE_TTL_SEC = 60  # cache window for repeated lookups within a tracking pass


def _last_price(ticker: str) -> float | None:
    """Best-effort latest price for a yfinance ticker (intraday, daily fallback)."""
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="5m")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
        hist = yf.Ticker(ticker).history(period="5d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as e:
        log.warning("Price fetch failed for %s: %s", ticker, e)
    return None


def get_market_snapshot() -> dict:
    """Compact market snapshot to ground AI call generation."""
    snap: dict = {}
    for label, tk in {
        "nifty50": "^NSEI",
        "banknifty": "^NSEBANK",
        "indiavix": "^INDIAVIX",
    }.items():
        px = _last_price(tk)
        if px is not None:
            snap[label] = px

    for label, tk in COMMODITY_PROXY.items():
        px = _last_price(tk)
        if px is not None:
            snap[label] = px

    try:
        snap.update(fetch_global_data())  # crude_brent, usd_inr, dxy
    except Exception as e:
        log.warning("Global data fetch failed: %s", e)

    log.debug("Advisory market snapshot: %s", snap)
    return snap


def _resolve_ticker(call: dict) -> str | None:
    """Map a call to a yfinance ticker, or None if not trackable for free."""
    cat = call.get("category")
    underlying = str(call.get("underlying") or "").upper().strip()
    if not underlying:
        return None

    # Option premiums and MCX commodity prices have no usable free feed.
    if cat in ("index_option", "commodity"):
        return None

    if cat in ("futures", "equity"):
        if underlying in INDEX_TICKERS:
            return INDEX_TICKERS[underlying]
        return f"{underlying}.NS"

    return None


def make_price_lookup():
    """Return a `price_lookup(call) -> float | None` with a short-lived cache."""
    cache: dict[str, tuple[float, float]] = {}

    def lookup(call: dict) -> float | None:
        ticker = _resolve_ticker(call)
        if ticker is None:
            return None
        now = time.time()
        cached = cache.get(ticker)
        if cached and (now - cached[1]) < _PRICE_TTL_SEC:
            return cached[0]
        px = _last_price(ticker)
        if px is not None:
            cache[ticker] = (px, now)
        return px

    return lookup
