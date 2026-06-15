"""
technicals.py — daily + intraday technical indicators to ground call generation
and the tape filter.

DATA SOURCE: official Dhan candles by default (real-time, the same feed you trade
on), via core.market_data_provider.get_historical_daily / get_historical_intraday.
Falls back to yfinance (delayed/unofficial) when no Dhan session is registered or a
Dhan history call fails. Toggle with TECHNICALS_USE_DHAN.

To use Dhan, the engine registers its (single) session once at startup via
set_data_session(); the tape filter and generators then get official data without
threading the session through every call, and without creating a second token.

Indicators:
  • Daily:    RSI(14), EMA20/50, ATR(14), day range %, 20-day range position, trend
  • Intraday: 5-min RSI/EMA9/EMA21/ATR, VWAP, opening range, 30m/15m momentum,
              position vs day high/low, last printed extreme, 5-min trend
"""

import time
from datetime import time as _dtime

from config.logger import get_logger
from config.settings import TECHNICALS_USE_DHAN

_SESSION_OPEN = _dtime(9, 15)
_SESSION_CLOSE = _dtime(15, 30)

log = get_logger("technicals")

# Underlying → yfinance ticker (fallback only).
_YF_TICKER = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
}

# Daily indicators barely move during a session; 5-min intraday refreshes ~every
# 5 min. Cache both so frequent callers (tape filter every poll, event-driven
# generation) don't refetch. Keyed by symbol → (data, fetched_at).
_DAILY_TTL_SEC = 1800     # 30 min
_INTRADAY_TTL_SEC = 60    # 1 min
_daily_cache: dict[str, tuple[dict, float]] = {}
_intraday_cache: dict[str, tuple[dict, float]] = {}

# Shared Dhan session, registered by the engine at startup (see set_data_session).
_SESSION = None


def set_data_session(session) -> None:
    """Register the engine's Dhan session so technicals can use official candles."""
    global _SESSION
    _SESSION = session
    log.info("Technicals data source: %s", "Dhan (official)" if session is not None
             and TECHNICALS_USE_DHAN else "yfinance (fallback)")


# ── indicator math ────────────────────────────────────────────────────────────

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
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
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


def _trend_intraday(last: float, ema_fast: float | None, ema_slow: float | None) -> str:
    """Intraday trend from the 9/21 EMA stack; price-vs-SLOW-EMA is the break."""
    if ema_fast is None or ema_slow is None:
        return "unknown"
    if ema_fast > ema_slow:
        return "up" if last >= ema_slow else "choppy"
    if ema_fast < ema_slow:
        return "down" if last <= ema_slow else "choppy"
    return "choppy"


# ── metrics from OHLC arrays (source-agnostic) ────────────────────────────────

def _daily_metrics(closes: list[float], highs: list[float], lows: list[float]) -> dict | None:
    if len(closes) < 20:
        return None
    close = round(closes[-1], 2)
    prev_close = closes[-2] if len(closes) >= 2 else close
    change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    day_high, day_low = round(highs[-1], 2), round(lows[-1], 2)
    range_pct = round((day_high - day_low) / close * 100, 2) if close else 0.0
    window = closes[-20:]
    hi20, lo20 = max(window), min(window)
    return {
        "last_close": close,
        "change_pct": change_pct,
        "rsi14": _rsi(closes, 14),
        "ema20": round(ema20, 2) if ema20 is not None else None,
        "ema50": round(ema50, 2) if ema50 is not None else None,
        "atr14": _atr(highs, lows, closes, 14),
        "day_high": day_high,
        "day_low": day_low,
        "range_pct": range_pct,
        "pct_from_20d_high": round((close - hi20) / hi20 * 100, 2) if hi20 else 0.0,
        "pct_from_20d_low": round((close - lo20) / lo20 * 100, 2) if lo20 else 0.0,
        "trend": _trend(close, ema20, ema50),
    }


def _intraday_metrics(opens, highs, lows, closes, vols) -> dict | None:
    if len(closes) < 3:
        return None
    last = round(closes[-1], 2)
    day_open = opens[0] if opens else closes[0]
    intraday_change_pct = round((last - day_open) / day_open * 100, 2) if day_open else 0.0
    ema9, ema21 = _ema(closes, 9), _ema(closes, 21)
    rsi14 = _rsi(closes, 14)
    atr14_5m = _atr(highs, lows, closes, 14)

    vwap = None
    if vols and sum(vols) > 0:
        tpv = sum(((highs[i] + lows[i] + closes[i]) / 3.0) * vols[i] for i in range(len(closes)))
        vwap = round(tpv / sum(vols), 2)
    vwap_state = ("above_vwap" if last >= vwap else "below_vwap") if vwap else None

    orn = min(3, len(highs))  # opening range = first 15 min (three 5-min bars)
    or_high, or_low = round(max(highs[:orn]), 2), round(min(lows[:orn]), 2)
    if last > or_high:
        or_state = "above_OR_high"
    elif last < or_low:
        or_state = "below_OR_low"
    else:
        or_state = "inside_OR"

    back = closes[-7] if len(closes) >= 7 else closes[0]
    mom_pct = round((last - back) / back * 100, 2) if back else 0.0
    back15 = closes[-4] if len(closes) >= 4 else closes[0]
    mom15_pct = round((last - back15) / back15 * 100, 2) if back15 else 0.0

    day_high, day_low = max(highs), min(lows)
    hi_idx = max(range(len(highs)), key=lambda i: highs[i])
    lo_idx = min(range(len(lows)), key=lambda i: lows[i])
    return {
        "last": last,
        "intraday_change_pct": intraday_change_pct,
        "pct_from_day_high": round((last - day_high) / day_high * 100, 2) if day_high else 0.0,
        "pct_from_day_low": round((last - day_low) / day_low * 100, 2) if day_low else 0.0,
        "last_extreme": "high" if hi_idx >= lo_idx else "low",
        "rsi14_5m": rsi14,
        "atr14_5m": atr14_5m,
        "ema9_5m": round(ema9, 2) if ema9 is not None else None,
        "ema21_5m": round(ema21, 2) if ema21 is not None else None,
        "vwap": vwap,
        "vwap_state": vwap_state,
        "opening_range_high": or_high,
        "opening_range_low": or_low,
        "opening_range_state": or_state,
        "momentum_30m_pct": mom_pct,
        "momentum_15m_pct": mom15_pct,
        "trend_5m": _trend_intraday(last, ema9, ema21),
        "bars": len(closes),
    }


# ── Dhan candle source ────────────────────────────────────────────────────────

def _compute_one_dhan(symbol: str, session) -> dict | None:
    from core.market_data_provider import get_historical_daily
    bars = get_historical_daily(session, symbol, days=120)
    if len(bars) < 20:
        return None
    return _daily_metrics([b["close"] for b in bars], [b["high"] for b in bars],
                          [b["low"] for b in bars])


def _compute_intraday_dhan(symbol: str, session) -> dict | None:
    from core.market_data_provider import get_historical_intraday
    bars = get_historical_intraday(session, symbol, interval="5", days=3)
    if not bars:
        return None
    # Latest calendar day's REGULAR session only — drops the after-hours/flat
    # placeholder bars Dhan appends (e.g. a 17:45 vol-0 bar).
    days = [b["ts"].date() for b in bars if b.get("ts")]
    if not days:
        return None
    today = max(days)
    g = sorted((b for b in bars if b.get("ts") and b["ts"].date() == today
                and _SESSION_OPEN <= b["ts"].time() <= _SESSION_CLOSE),
               key=lambda b: b["ts"])
    if len(g) < 3:
        return None
    return _intraday_metrics([b["open"] for b in g], [b["high"] for b in g],
                             [b["low"] for b in g], [b["close"] for b in g],
                             [b["volume"] for b in g])


# ── yfinance candle source (fallback) ─────────────────────────────────────────

def _compute_one_yf(symbol: str, ticker: str) -> dict | None:
    import yfinance as yf
    try:
        hist = yf.Ticker(ticker).history(period="3mo", interval="1d")
    except Exception as e:  # noqa: BLE001
        log.warning("yfinance daily fetch failed for %s (%s): %s", symbol, ticker, e)
        return None
    if hist is None or hist.empty or len(hist) < 20:
        return None
    return _daily_metrics([float(x) for x in hist["Close"]], [float(x) for x in hist["High"]],
                          [float(x) for x in hist["Low"]])


def _compute_intraday_yf(symbol: str, ticker: str) -> dict | None:
    import yfinance as yf
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="5m")
    except Exception as e:  # noqa: BLE001
        log.warning("yfinance intraday fetch failed for %s (%s): %s", symbol, ticker, e)
        return None
    if hist is None or hist.empty or len(hist) < 3:
        return None
    try:
        vols = [float(x) for x in hist["Volume"]]
    except Exception:  # noqa: BLE001
        vols = []
    return _intraday_metrics([float(x) for x in hist["Open"]], [float(x) for x in hist["High"]],
                             [float(x) for x in hist["Low"]], [float(x) for x in hist["Close"]], vols)


# ── public API (Dhan-first, yfinance fallback, cached) ────────────────────────

def _resolve(symbol: str, session, dhan_fn, yf_fn) -> dict | None:
    """Try official Dhan candles, then yfinance. Returns the metrics dict or None."""
    if TECHNICALS_USE_DHAN and session is not None:
        try:
            data = dhan_fn(symbol, session)
            if data:
                return data
        except Exception as e:  # noqa: BLE001
            log.debug("Dhan technicals failed for %s (%s); falling back to yfinance.", symbol, e)
    ticker = _YF_TICKER.get(symbol)
    return yf_fn(symbol, ticker) if ticker else None


def get_technicals(symbols: list[str] | None = None, session=None) -> dict[str, dict]:
    """DAILY technical context per underlying (cached ~30 min)."""
    session = session if session is not None else _SESSION
    syms = symbols or ["NIFTY", "BANKNIFTY"]
    out: dict[str, dict] = {}
    now = time.time()
    for sym in syms:
        sym = sym.upper()
        hit = _daily_cache.get(sym)
        if hit and (now - hit[1]) < _DAILY_TTL_SEC:
            out[sym] = hit[0]
            continue
        data = _resolve(sym, session, _compute_one_dhan, _compute_one_yf)
        if data:
            _daily_cache[sym] = (data, now)
            out[sym] = data
    return out


def get_intraday_technicals(symbols: list[str] | None = None, session=None) -> dict[str, dict]:
    """INTRADAY (5-min) technical context per underlying (cached ~60s)."""
    session = session if session is not None else _SESSION
    syms = symbols or ["NIFTY", "BANKNIFTY"]
    out: dict[str, dict] = {}
    now = time.time()
    for sym in syms:
        sym = sym.upper()
        hit = _intraday_cache.get(sym)
        if hit and (now - hit[1]) < _INTRADAY_TTL_SEC:
            out[sym] = hit[0]
            continue
        data = _resolve(sym, session, _compute_intraday_dhan, _compute_intraday_yf)
        if data:
            _intraday_cache[sym] = (data, now)
            out[sym] = data
    return out
