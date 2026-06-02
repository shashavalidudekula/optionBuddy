"""
technicals.py — Daily technical indicators to ground AI call generation.

The advisory engine previously reasoned over a single live price point. That's a
thin basis for a trade. This module computes real technical context from daily
OHLC (via yfinance) so the model reasons over trend, momentum and volatility:

  • RSI(14)            — momentum / overbought-oversold
  • EMA(20), EMA(50)   — trend structure
  • ATR(14)            — volatility (typical daily move, for stop sizing)
  • day range %        — today's high-low as % of close
  • dist from 20d hi/lo — where price sits in its recent range
  • trend label        — quick human-readable read

Everything degrades gracefully: if yfinance is unavailable for a symbol, that
symbol is simply omitted from the result.
"""

import time

import yfinance as yf

from config.logger import get_logger

log = get_logger("technicals")

# Underlying → yfinance ticker for the index spot.
_YF_TICKER = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
}

# Daily indicators barely move during a session; 5-min intraday bars refresh every
# ~5 min. Cache both so event-driven generation (every ~1-2 min) doesn't hammer
# yfinance. Keyed by symbol → (data, fetched_at).
_DAILY_TTL_SEC = 1800     # 30 min
_INTRADAY_TTL_SEC = 60    # 1 min
_daily_cache: dict[str, tuple[dict, float]] = {}
_intraday_cache: dict[str, tuple[dict, float]] = {}


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period  # seed with SMA
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    # Wilder's smoothing
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 2)


def _trend(close: float, ema20: float | None, ema50: float | None) -> str:
    if ema20 is None or ema50 is None:
        return "unknown"
    if close > ema20 > ema50:
        return "uptrend"
    if close < ema20 < ema50:
        return "downtrend"
    if close > ema20 and close > ema50:
        return "above MAs (mixed)"
    if close < ema20 and close < ema50:
        return "below MAs (mixed)"
    return "sideways"


def _compute_one(symbol: str, ticker: str) -> dict | None:
    try:
        hist = yf.Ticker(ticker).history(period="3mo", interval="1d")
    except Exception as e:  # noqa: BLE001
        log.warning("OHLC fetch failed for %s (%s): %s", symbol, ticker, e)
        return None
    if hist is None or hist.empty or len(hist) < 20:
        log.warning("Insufficient OHLC for %s (%s)", symbol, ticker)
        return None

    closes = [float(x) for x in hist["Close"].tolist()]
    highs = [float(x) for x in hist["High"].tolist()]
    lows = [float(x) for x in hist["Low"].tolist()]

    close = round(closes[-1], 2)
    prev_close = closes[-2] if len(closes) >= 2 else close
    change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0

    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    atr14 = _atr(highs, lows, closes, 14)
    rsi14 = _rsi(closes, 14)

    day_high = round(highs[-1], 2)
    day_low = round(lows[-1], 2)
    range_pct = round((day_high - day_low) / close * 100, 2) if close else 0.0

    window = closes[-20:]
    hi20, lo20 = max(window), min(window)
    dist_hi = round((close - hi20) / hi20 * 100, 2) if hi20 else 0.0   # <=0 (below high)
    dist_lo = round((close - lo20) / lo20 * 100, 2) if lo20 else 0.0   # >=0 (above low)

    return {
        "last_close": close,
        "change_pct": change_pct,
        "rsi14": rsi14,
        "ema20": round(ema20, 2) if ema20 is not None else None,
        "ema50": round(ema50, 2) if ema50 is not None else None,
        "atr14": atr14,
        "day_high": day_high,
        "day_low": day_low,
        "range_pct": range_pct,
        "pct_from_20d_high": dist_hi,
        "pct_from_20d_low": dist_lo,
        "trend": _trend(close, ema20, ema50),
    }


def _trend_intraday(last: float, ema_fast: float | None, ema_slow: float | None) -> str:
    if ema_fast is None or ema_slow is None:
        return "unknown"
    if last > ema_fast > ema_slow:
        return "up"
    if last < ema_fast < ema_slow:
        return "down"
    return "choppy"


def _compute_intraday(symbol: str, ticker: str) -> dict | None:
    """Intraday (5-min) context for today's session."""
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="5m")
    except Exception as e:  # noqa: BLE001
        log.warning("Intraday fetch failed for %s (%s): %s", symbol, ticker, e)
        return None
    if hist is None or hist.empty or len(hist) < 3:
        return None

    closes = [float(x) for x in hist["Close"].tolist()]
    highs = [float(x) for x in hist["High"].tolist()]
    lows = [float(x) for x in hist["Low"].tolist()]
    opens = [float(x) for x in hist["Open"].tolist()]
    try:
        vols = [float(x) for x in hist["Volume"].tolist()]
    except Exception:  # noqa: BLE001
        vols = []

    last = round(closes[-1], 2)
    day_open = opens[0] if opens else closes[0]
    intraday_change_pct = round((last - day_open) / day_open * 100, 2) if day_open else 0.0

    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    rsi14 = _rsi(closes, 14)  # None until ~15 bars exist

    # VWAP (only meaningful when the feed carries volume).
    vwap = None
    if vols and sum(vols) > 0:
        tpv = sum(((highs[i] + lows[i] + closes[i]) / 3.0) * vols[i] for i in range(len(closes)))
        vwap = round(tpv / sum(vols), 2)
    vwap_state = None
    if vwap:
        vwap_state = "above_vwap" if last >= vwap else "below_vwap"

    # Opening range = first 15 min (three 5-min bars).
    orn = min(3, len(highs))
    or_high = round(max(highs[:orn]), 2)
    or_low = round(min(lows[:orn]), 2)
    if last > or_high:
        or_state = "above_OR_high"
    elif last < or_low:
        or_state = "below_OR_low"
    else:
        or_state = "inside_OR"

    # 30-min momentum (6 bars back).
    back = closes[-7] if len(closes) >= 7 else closes[0]
    mom_pct = round((last - back) / back * 100, 2) if back else 0.0

    return {
        "last": last,
        "intraday_change_pct": intraday_change_pct,
        "rsi14_5m": rsi14,
        "ema9_5m": round(ema9, 2) if ema9 is not None else None,
        "ema21_5m": round(ema21, 2) if ema21 is not None else None,
        "vwap": vwap,
        "vwap_state": vwap_state,
        "opening_range_high": or_high,
        "opening_range_low": or_low,
        "opening_range_state": or_state,
        "momentum_30m_pct": mom_pct,
        "trend_5m": _trend_intraday(last, ema9, ema21),
        "bars": len(closes),
    }


def get_technicals(symbols: list[str] | None = None) -> dict[str, dict]:
    """Return DAILY technical context per underlying (cached ~30 min).

    Returns:
        {symbol: {last_close, change_pct, rsi14, ema20, ema50, atr14,
                  day_high, day_low, range_pct, pct_from_20d_high,
                  pct_from_20d_low, trend}} — symbols that fail are omitted.
    """
    syms = symbols or ["NIFTY", "BANKNIFTY"]
    out: dict[str, dict] = {}
    now = time.time()
    for sym in syms:
        sym = sym.upper()
        ticker = _YF_TICKER.get(sym)
        if not ticker:
            continue
        hit = _daily_cache.get(sym)
        if hit and (now - hit[1]) < _DAILY_TTL_SEC:
            out[sym] = hit[0]
            continue
        data = _compute_one(sym, ticker)
        if data:
            _daily_cache[sym] = (data, now)
            out[sym] = data
    return out


def get_intraday_technicals(symbols: list[str] | None = None) -> dict[str, dict]:
    """Return INTRADAY (5-min) technical context per underlying (cached ~60s).

    Returns:
        {symbol: {last, intraday_change_pct, rsi14_5m, ema9_5m, ema21_5m, vwap,
                  vwap_state, opening_range_high/low/state, momentum_30m_pct,
                  trend_5m, bars}} — symbols that fail are omitted.
    """
    syms = symbols or ["NIFTY", "BANKNIFTY"]
    out: dict[str, dict] = {}
    now = time.time()
    for sym in syms:
        sym = sym.upper()
        ticker = _YF_TICKER.get(sym)
        if not ticker:
            continue
        hit = _intraday_cache.get(sym)
        if hit and (now - hit[1]) < _INTRADAY_TTL_SEC:
            out[sym] = hit[0]
            continue
        data = _compute_intraday(sym, ticker)
        if data:
            _intraday_cache[sym] = (data, now)
            out[sym] = data
    return out
