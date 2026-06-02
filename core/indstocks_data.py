"""
indstocks_data.py — INDstocks market-data layer for the advisory product.

DATA-ONLY use of the INDstocks API (no order execution). It powers:
  • the market snapshot that grounds AI call generation, and
  • the live price / option-premium lookup that the call tracker uses.

INDstocks API (https://api-docs.indstocks.com):
  • GET /market/instruments?source=equity|fno|index   → CSV instrument master
      columns: EXCH, SEGMENT, SECURITY_ID, INSTRUMENT_NAME, TRADING_SYMBOL,
               CUSTOM_SYMBOL, LOT_UNITS, STRIKE_PRICE, OPTION_TYPE, EXPIRY_DATE,
               EXPIRY_CODE, TICK_SIZE, SYMBOL_NAME
  • GET /market/quotes/ltp?scrip-codes=NSE_3045,NFO_51011
      → {"status":"success","data":{"NSE_3045":{"live_price":792.5}, ...}}

A "scrip-code" is "<SEGMENT>_<SECURITY_ID>", e.g. NSE_3045 (cash), NFO_51011 (F&O).

Everything degrades gracefully: if an instrument can't be resolved or the API
is unavailable, lookups return None and the snapshot falls back to global cues.
"""

import csv
import io
import re
import time
from datetime import datetime, date

from config.logger import get_logger
from core.market_data import fetch_global_data

log = get_logger("indstocks_data")

# The LTP endpoint 400s once the request URL gets too long (~470 codes blew it),
# so keep batches small; ATM-trimmed option requests are far under this anyway.
_LTP_BATCH = 50
_PRICE_TTL_SEC = 4           # short: shares a price within one ~5s poll, fresh next poll
_INSTRUMENTS_TTL_SEC = 6 * 3600  # refresh the master a few times a day

# Index symbol → name as it (most likely) appears in the index master SYMBOL_NAME.
_INDEX_ALIASES = {
    "NIFTY": ("NIFTY", "NIFTY 50", "NIFTY50"),
    "BANKNIFTY": ("BANKNIFTY", "BANK NIFTY", "NIFTY BANK"),
    "FINNIFTY": ("FINNIFTY", "NIFTY FIN SERVICE", "FIN NIFTY"),
    "INDIAVIX": ("INDIA VIX", "INDIAVIX"),
}

# ── module-level cache ────────────────────────────────────────────────────────
_master: dict[str, list[dict]] = {}   # source → list of row dicts
_master_ts: float = 0.0
_seen_segments: set[tuple[str, str]] = set()


# ── instruments master ────────────────────────────────────────────────────────

def _fetch_csv(session, source: str) -> list[dict]:
    """Download one instruments-master CSV and parse to list of dict rows."""
    try:
        resp = session.session.get(
            session.base_url + "/market/instruments",
            params={"source": source},
            timeout=30,
        )
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        # Normalise keys to UPPER for resilience against header casing.
        norm = [{(k or "").strip().upper(): (v or "").strip() for k, v in r.items()} for r in rows]
        for r in norm:
            _seen_segments.add((r.get("EXCH", ""), r.get("SEGMENT", "")))
        log.info("Loaded %s instruments from source=%s", len(norm), source)
        return norm
    except Exception as e:
        log.warning("Instruments master fetch failed (source=%s): %s", source, e)
        return []


def _load_master(session) -> None:
    """(Re)load all instrument masters into the module cache if stale."""
    global _master, _master_ts
    if session is None:
        return
    if _master and (time.time() - _master_ts) < _INSTRUMENTS_TTL_SEC:
        return
    _master = {src: _fetch_csv(session, src) for src in ("index", "equity", "fno")}
    _master_ts = time.time()
    if _seen_segments:
        log.info("INDstocks (EXCH,SEGMENT) seen: %s", sorted(_seen_segments))


def _scrip_prefix(exch: str, segment: str) -> str:
    """Map an instrument's (EXCH, SEGMENT) to its scrip-code prefix."""
    exch = (exch or "").upper().strip()
    seg = (segment or "").upper().strip()
    if exch == "NSE":
        if seg in ("FNO", "FO", "F&O", "D"):
            return "NFO"
        if seg in ("CUR", "CURRENCY", "CDS"):
            return "CDS"
        return "NSE"  # cash equity and index spot
    if exch == "BSE":
        if seg in ("FNO", "FO", "F&O", "D"):
            return "BFO"
        return "BSE"
    if exch == "MCX":
        return "MCX"
    return exch or "NSE"


def _scrip_code(row: dict) -> str | None:
    sid = row.get("SECURITY_ID")
    if not sid:
        return None
    return f"{_scrip_prefix(row.get('EXCH', ''), row.get('SEGMENT', ''))}_{sid}"


def _parse_expiry(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    # INDstocks F&O master uses "MM/DD/YYYY HH:MM"; keep the older formats too.
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%Y/%m/%d", "%d-%b-%y", "%d%b%Y",
                "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _fno_underlying(row: dict) -> str:
    """Underlying ticker for an F&O row.

    NSE F&O rows leave SYMBOL_NAME blank and encode the underlying as the prefix
    of TRADING_SYMBOL, e.g. 'NIFTY-Jun2026-23000-PE' → 'NIFTY',
    'BANKNIFTY-Jun2026-FUT' → 'BANKNIFTY'. CUSTOM_SYMBOL ('NIFTY 30 JUN ...') is a
    fallback; SYMBOL_NAME (used by some BSE rows) is the last resort.
    """
    ts = (row.get("TRADING_SYMBOL") or "").strip()
    if "-" in ts:
        return ts.split("-", 1)[0].upper()
    cs = (row.get("CUSTOM_SYMBOL") or "").strip()
    if cs:
        return cs.split(" ", 1)[0].upper()
    return (row.get("SYMBOL_NAME") or "").upper()


def _nearest_expiry(rows: list[dict], today: date) -> str | None:
    """Pick the soonest non-expired EXPIRY_DATE among the given rows."""
    dated = []
    for r in rows:
        d = _parse_expiry(r.get("EXPIRY_DATE", ""))
        if d and d >= today:
            dated.append((d, r.get("EXPIRY_DATE")))
    if not dated:
        return None
    dated.sort(key=lambda t: t[0])
    return dated[0][1]


# ── resolution ────────────────────────────────────────────────────────────────

def _equity_scrip(underlying: str) -> str | None:
    u = underlying.upper().strip()
    for r in _master.get("equity", []):
        if r.get("SYMBOL_NAME", "").upper() == u or r.get("TRADING_SYMBOL", "").upper() == u:
            return _scrip_code(r)
    return None


def _index_scrip(underlying: str) -> str | None:
    aliases = _INDEX_ALIASES.get(underlying.upper().strip(), (underlying.upper().strip(),))
    for r in _master.get("index", []):
        # The index master only has EXCH/SECURITY_ID/SEGMENT — the index NAME is in
        # SEGMENT (e.g. "NIFTY 50", "BANK NIFTY", "India VIX"). Keep SYMBOL_NAME/
        # TRADING_SYMBOL as fallbacks in case the feed shape changes.
        seg = (r.get("SEGMENT") or "").upper()
        name = (r.get("SYMBOL_NAME") or "").upper()
        sym = (r.get("TRADING_SYMBOL") or "").upper()
        if seg in aliases or name in aliases or sym in aliases:
            return _scrip_code(r)
    return None


def _future_scrip(underlying: str) -> str | None:
    u = underlying.upper().strip()
    rows = [
        r for r in _master.get("fno", [])
        if _fno_underlying(r) == u
        and "FUT" in (r.get("INSTRUMENT_NAME") or "").upper()
    ]
    if not rows:
        return None
    exp = _nearest_expiry(rows, date.today())
    for r in rows:
        if r.get("EXPIRY_DATE") == exp:
            return _scrip_code(r)
    return _scrip_code(rows[0])


def _option_scrip(underlying: str, strike: float, opt_type: str, expiry: str | None = None) -> str | None:
    u = underlying.upper().strip()
    ot = opt_type.upper().strip()
    rows = [
        r for r in _master.get("fno", [])
        if _fno_underlying(r) == u
        and r.get("OPTION_TYPE", "").upper() == ot
    ]
    if not rows:
        return None
    exp = expiry or _nearest_expiry(rows, date.today())

    def _strike_match(r):
        try:
            return abs(float(r.get("STRIKE_PRICE", "0")) - strike) < 0.5
        except (TypeError, ValueError):
            return False

    candidates = [r for r in rows if r.get("EXPIRY_DATE") == exp and _strike_match(r)]
    if not candidates:
        candidates = [r for r in rows if _strike_match(r)]
    return _scrip_code(candidates[0]) if candidates else None


# Strike + option type, tolerant of separators so both "NIFTY 23400 PE" and the
# trading-symbol form "NIFTY-Jun2026-23400-PE" resolve (the year 2026 won't match
# because it isn't immediately followed by CE/PE).
_OPTION_RE = re.compile(r"(\d{3,7})[\s\-]*(CE|PE)\b", re.IGNORECASE)


def resolve_scrip_for_call(call: dict) -> str | None:
    """Map an advisory call to an INDstocks scrip-code, or None if unresolved."""
    cat = call.get("category")
    underlying = str(call.get("underlying") or "").strip()
    instrument = str(call.get("instrument") or "").strip()
    if not underlying:
        return None

    if cat == "index_option":
        m = _OPTION_RE.search(instrument)
        if not m:
            return None
        strike = float(m.group(1))
        return _option_scrip(underlying, strike, m.group(2))

    if cat == "futures":
        return _future_scrip(underlying) or _index_scrip(underlying) or _equity_scrip(underlying)

    if cat == "equity":
        return _equity_scrip(underlying) or _index_scrip(underlying)

    if cat == "commodity":
        # MCX resolution needs the commodity master/segment; not wired yet.
        return None
    return None


# ── quotes ────────────────────────────────────────────────────────────────────

def get_ltp(session, scrip_codes: list[str]) -> dict[str, float]:
    """Batched LTP for scrip-codes → {scrip_code: live_price}."""
    out: dict[str, float] = {}
    if session is None:
        return out
    uniq = [s for s in dict.fromkeys(scrip_codes) if s]
    for i in range(0, len(uniq), _LTP_BATCH):
        batch = uniq[i:i + _LTP_BATCH]
        try:
            resp = session.get("/market/quotes/ltp", params={"scrip-codes": ",".join(batch)})
            data = resp.get("data", {}) if isinstance(resp, dict) else {}
            for code, payload in (data or {}).items():
                price = None
                if isinstance(payload, dict):
                    price = payload.get("live_price", payload.get("last_traded_price"))
                elif isinstance(payload, (int, float)):
                    price = payload
                if price is not None:
                    try:
                        out[code] = float(price)
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            log.warning("LTP fetch failed for %s codes: %s", len(batch), e)
    return out


# ── public API ────────────────────────────────────────────────────────────────

def get_market_snapshot(session) -> dict:
    """Index spot levels (INDstocks) + global macro cues (yfinance) for grounding."""
    _load_master(session)
    snap: dict = {}

    index_codes = {}
    for key in ("NIFTY", "BANKNIFTY", "INDIAVIX"):
        code = _index_scrip(key)
        if code:
            index_codes[key.lower()] = code
    if index_codes:
        prices = get_ltp(session, list(index_codes.values()))
        for label, code in index_codes.items():
            if code in prices:
                snap[label] = prices[code]

    try:
        snap.update(fetch_global_data())  # crude_brent, usd_inr, dxy
    except Exception as e:
        log.warning("Global data fetch failed: %s", e)

    # Technical grounding so the model reasons over real indicators, not just a
    # single live price point: daily structure + live intraday (5-min) context.
    try:
        from core.technicals import get_technicals, get_intraday_technicals
        tech = get_technicals(["NIFTY", "BANKNIFTY"])
        if tech:
            snap["technicals"] = tech
        intraday = get_intraday_technicals(["NIFTY", "BANKNIFTY"])
        if intraday:
            snap["intraday"] = intraday
    except Exception as e:
        log.warning("Technicals fetch failed: %s", e)

    log.debug("Advisory market snapshot: %s", snap)
    return snap


def get_index_spots(session) -> dict[str, float]:
    """Cheap live spot for NIFTY / BANKNIFTY / INDIA VIX (for generation triggers)."""
    _load_master(session)
    codes = {}
    for key in ("NIFTY", "BANKNIFTY", "INDIAVIX"):
        c = _index_scrip(key)
        if c:
            codes[key.lower()] = c
    if not codes:
        return {}
    prices = get_ltp(session, list(codes.values()))
    return {label: prices[c] for label, c in codes.items() if c in prices}


def get_option_chain(session, underlying: str, count: int = 6) -> list[dict]:
    """Nearest-expiry option chain around ATM, with live premiums.

    Returns rows: {strike, option_type, scrip_code, premium, expiry, trading_symbol}.
    Used to ground option-call generation in real, tradeable premiums.
    """
    _load_master(session)
    u = underlying.upper().strip()
    rows = [r for r in _master.get("fno", [])
            if _fno_underlying(r) == u and r.get("OPTION_TYPE", "").upper() in ("CE", "PE")]
    if not rows:
        return []

    exp = _nearest_expiry(rows, date.today())
    rows = [r for r in rows if r.get("EXPIRY_DATE") == exp]
    if not rows:
        return []

    spot_code = _index_scrip(u) or _equity_scrip(u)
    spot = get_ltp(session, [spot_code]).get(spot_code) if spot_code else None

    def _strike(r):
        try:
            return float(r.get("STRIKE_PRICE", "0"))
        except (TypeError, ValueError):
            return 0.0

    strikes = sorted({_strike(r) for r in rows if _strike(r) > 0})
    if spot and strikes:
        atm = min(strikes, key=lambda s: abs(s - spot))
        idx = strikes.index(atm)
        lo, hi = max(0, idx - count), idx + count + 1
        wanted = set(strikes[lo:hi])
        rows = [r for r in rows if _strike(r) in wanted]

    codes = [c for c in (_scrip_code(r) for r in rows) if c]
    prices = get_ltp(session, codes)
    chain = []
    for r in rows:
        code = _scrip_code(r)
        chain.append({
            "strike": _strike(r),
            "option_type": r.get("OPTION_TYPE", "").upper(),
            "scrip_code": code,
            "premium": prices.get(code),
            "expiry": r.get("EXPIRY_DATE"),
            "trading_symbol": r.get("TRADING_SYMBOL"),
        })
    chain.sort(key=lambda c: (c["strike"], c["option_type"]))
    return chain


def get_lot_size(session, underlying: str) -> int | None:
    """Lot size (LOT_UNITS) for an F&O underlying from the master; None if unknown.

    Falls back to PAPER_LOT_SIZES when the master can't be loaded so the paper
    trader still works without a live session.
    """
    from config.settings import PAPER_LOT_SIZES

    u = underlying.upper().strip()
    try:
        _load_master(session)
        for r in _master.get("fno", []):
            if _fno_underlying(r) == u:
                lot = r.get("LOT_UNITS") or r.get("LOT_SIZE")
                if lot:
                    try:
                        val = int(float(lot))
                        if val > 0:
                            return val
                    except (TypeError, ValueError):
                        pass
                break
    except Exception as e:  # noqa: BLE001
        log.warning("Lot-size master lookup failed for %s: %s", u, e)
    return PAPER_LOT_SIZES.get(u)


def make_price_lookup(session):
    """Return `price_lookup(call) -> float | None` backed by INDstocks LTP."""
    cache: dict[str, tuple[float, float]] = {}

    def lookup(call: dict) -> float | None:
        code = resolve_scrip_for_call(call)
        if not code:
            return None
        now = time.time()
        hit = cache.get(code)
        if hit and (now - hit[1]) < _PRICE_TTL_SEC:
            return hit[0]
        price = get_ltp(session, [code]).get(code)
        if price is not None:
            cache[code] = (price, now)
        return price

    return lookup
