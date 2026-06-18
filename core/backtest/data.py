"""
data.py — historical bar loading + on-disk cache for the backtester.

Fetches daily OHLC via the existing provider facade (core/market_data_provider →
Dhan /charts) ONCE, caches to JSON, then serves every later backtest from disk —
so runs are fast, reproducible, and work offline (no session needed after the
first populate). NIFTY/BANKNIFTY/INDIAVIX/equities all go through the same path.

Bars are normalised to {"date": "YYYY-MM-DD", open, high, low, close, volume},
oldest-first. INDIAVIX bars carry the implied-vol level in `close` (in vol POINTS,
e.g. 13.8 — divide by 100 for a fraction).
"""
import json
import os
from datetime import date, datetime

from config.logger import get_logger
from core.market_data_provider import get_historical_daily

log = get_logger("backtest_data")

CACHE_DIR = os.path.join("logs", "bt_cache")


def _cache_path(underlying: str, interval: str = "1d") -> str:
    safe = "".join(c for c in str(underlying).upper() if c.isalnum() or c in ("_", "-"))
    return os.path.join(CACHE_DIR, f"{safe}_{interval}.json")


def _to_record(bar: dict) -> dict | None:
    """Normalise a provider bar (ts=datetime) → {date, ohlcv} with a date string."""
    ts = bar.get("ts")
    if isinstance(ts, datetime):
        d = ts.date().isoformat()
    elif isinstance(ts, str):
        d = ts[:10]
    else:
        return None
    try:
        return {"date": d, "open": float(bar["open"]), "high": float(bar["high"]),
                "low": float(bar["low"]), "close": float(bar["close"]),
                "volume": float(bar.get("volume") or 0.0)}
    except (KeyError, TypeError, ValueError):
        return None


def _read_cache(path: str) -> list[dict] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        log.warning("Backtest cache read failed (%s): %s", path, e)
        return None


def _write_cache(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Backtest cache write failed (%s): %s", path, e)


def _filter(rows: list[dict], from_date, to_date) -> list[dict]:
    lo = from_date.isoformat() if isinstance(from_date, date) else from_date
    hi = to_date.isoformat() if isinstance(to_date, date) else to_date
    out = rows
    if lo:
        out = [r for r in out if r["date"] >= lo]
    if hi:
        out = [r for r in out if r["date"] <= hi]
    return out


def load_daily(session, underlying: str, from_date=None, to_date=None,
               refresh: bool = False) -> list[dict]:
    """Daily bars for `underlying`, oldest-first, from cache or the live feed.

    refresh=True forces a re-fetch (and re-cache). Without a session and with no
    cache, returns [] (the backtest can't run until data is populated once)."""
    path = _cache_path(underlying)
    if not refresh:
        cached = _read_cache(path)
        if cached:
            return _filter(cached, from_date, to_date)
    if session is None:
        log.warning("No cache and no session for %s — cannot load history.", underlying)
        return []
    # Dhan caps the per-request span, so pull a long window in ~1y chunks.
    raw = get_historical_daily(session, underlying, from_date=from_date, to_date=to_date)
    rows = [r for r in (_to_record(b) for b in (raw or [])) if r]
    rows.sort(key=lambda r: r["date"])
    if rows:
        _write_cache(path, rows)
    return _filter(rows, from_date, to_date)


def align(series_a: list[dict], series_b: list[dict]) -> list[tuple]:
    """Inner-join two bar series on date → [(date, bar_a, bar_b)] for common dates,
    oldest-first. Used to pair an underlying with its IV (VIX) series, or two legs
    of a pair trade."""
    by_b = {r["date"]: r for r in series_b}
    out = []
    for r in series_a:
        b = by_b.get(r["date"])
        if b is not None:
            out.append((r["date"], r, b))
    out.sort(key=lambda t: t[0])
    return out
