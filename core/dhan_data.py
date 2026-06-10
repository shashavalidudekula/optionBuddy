"""
dhan_data.py — DhanHQ v2 market-data layer (data only — no order execution).

Drop-in for indstocks_data: it exposes the SAME public functions the advisory
orchestrator and paper trader call, so switching providers is just a setting
(MARKET_DATA_PROVIDER) behind core/market_data_provider.py.

Why Dhan over INDstocks for an options advisory:
  • ONE /optionchain call returns the full chain WITH greeks (δ/γ/θ/ν), IV, OI,
    volume and bid/ask per strike — no per-strike LTP fan-out.
  • marketfeed/ltp returns LTP for up to 1000 instruments grouped by segment.

DhanHQ v2 endpoints used (https://dhanhq.co/docs/v2):
  • POST /optionchain            {UnderlyingScrip, UnderlyingSeg, Expiry}
       → {data:{last_price, oc:{"<strike>":{ce:{...},pe:{...}}}}}
  • POST /optionchain/expirylist {UnderlyingScrip, UnderlyingSeg}
       → {data:["YYYY-MM-DD", ...]}
  • POST /marketfeed/ltp         {"<SEG>":[<security_id>, ...]}
       → {data:{"<SEG>":{"<security_id>":{last_price}}}}
  • Scrip master CSV (instrument reference) from DHAN_SCRIP_MASTER_URL.

A "scrip-code" in this module is "<EXCHANGE_SEGMENT>:<SECURITY_ID>", e.g.
"NSE_FNO:42528", "IDX_I:13". get_ltp() regroups these into Dhan's request shape.

Everything degrades gracefully: unresolved instrument / feed down → None, and the
snapshot falls back to global cues.

Rate limits respected via short caches: optionchain is 1 req / 3s per
underlying-expiry, marketfeed is 1 req/s.
"""

import csv
import io
import re
import threading
import time
from datetime import datetime, date

import requests

from config.logger import get_logger
from config.settings import (
    DHAN_SCRIP_MASTER_URL, DHAN_INDEX_UNDERLYINGS, PAPER_LOT_SIZES,
)
from core.market_data import fetch_global_data

log = get_logger("dhan_data")

# ── tunables ────────────────────────────────────────────────────────────────
_LTP_BATCH = 500                 # Dhan allows up to 1000 instruments/request
_PRICE_TTL_SEC = 4               # share a price within one ~5s poll, fresh next
_CHAIN_TTL_SEC = 3               # optionchain limit is 1 req / 3s per underlying
_EXPIRY_TTL_SEC = 3600           # expiry list changes at most daily
_INSTRUMENTS_TTL_SEC = 6 * 3600  # refresh the scrip master a few times a day

# Dhan quote API hard limit is 1 request/second. A process-wide throttle spaces
# ALL marketfeed/ltp calls (index spots + price tracking, across worker threads)
# so they don't 429 and silently drop ticks — dropped ticks would miss T1/SL exits.
_LTP_MIN_INTERVAL = 1.05
_ltp_lock = threading.Lock()
_ltp_last_ts = 0.0

# Index aliases → canonical key in DHAN_INDEX_UNDERLYINGS.
_INDEX_ALIASES = {
    "NIFTY": "NIFTY", "NIFTY 50": "NIFTY", "NIFTY50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY", "BANK NIFTY": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY", "NIFTY FIN SERVICE": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX", "BSE SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
    "INDIAVIX": "INDIAVIX", "INDIA VIX": "INDIAVIX",
}

# F&O segment an underlying's options live in (NSE indices/stocks vs BSE indices).
_BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}

# Strike + option type, tolerant of separators ("NIFTY 23400 PE", "NIFTY-...-23400-PE").
_OPTION_RE = re.compile(r"(\d{3,7})[\s\-]*(CE|PE)\b", re.IGNORECASE)

# ── module caches ────────────────────────────────────────────────────────────
_master: list[dict] = []          # scrip-master rows (normalised UPPER keys)
_master_ts: float = 0.0
_chain_cache: dict = {}           # (underlying, expiry) → (rows, ts)
_expiry_cache: dict = {}          # underlying → (expiries, ts)


# ── segment / scrip-code helpers ──────────────────────────────────────────────

def _exchange_segment(exch: str, seg_letter: str) -> str:
    """Map (SEM_EXM_EXCH_ID, SEM_SEGMENT) → Dhan exchange-segment string."""
    exch = (exch or "").upper().strip()
    seg = (seg_letter or "").upper().strip()
    table = {
        ("NSE", "E"): "NSE_EQ", ("NSE", "D"): "NSE_FNO",
        ("NSE", "C"): "NSE_CURRENCY", ("NSE", "I"): "IDX_I",
        ("BSE", "E"): "BSE_EQ", ("BSE", "D"): "BSE_FNO",
        ("BSE", "C"): "BSE_CURRENCY", ("BSE", "I"): "IDX_I",
        ("MCX", "M"): "MCX_COMM", ("MCX", "D"): "MCX_COMM",
    }
    return table.get((exch, seg), f"{exch}_EQ" if exch else "NSE_EQ")


def _fno_segment_for(underlying: str) -> str:
    return "BSE_FNO" if underlying.upper().strip() in _BSE_UNDERLYINGS else "NSE_FNO"


def _scrip_code(row: dict) -> str | None:
    sid = row.get("SEM_SMST_SECURITY_ID") or row.get("SECURITY_ID")
    if not sid:
        return None
    seg = _exchange_segment(row.get("SEM_EXM_EXCH_ID", ""), row.get("SEM_SEGMENT", ""))
    return f"{seg}:{sid}"


# ── scrip master (instrument reference) ──────────────────────────────────────

def _load_master(session=None) -> None:
    """(Re)load the Dhan scrip master CSV into the module cache if stale."""
    global _master, _master_ts
    if _master and (time.time() - _master_ts) < _INSTRUMENTS_TTL_SEC:
        return
    try:
        resp = requests.get(DHAN_SCRIP_MASTER_URL, timeout=60)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        _master = [{(k or "").strip().upper(): (v or "").strip() for k, v in r.items()} for r in rows]
        _master_ts = time.time()
        log.info("Loaded %s instruments from Dhan scrip master", len(_master))
    except Exception as e:  # noqa: BLE001
        log.warning("Dhan scrip master fetch failed: %s", e)


def _parse_expiry(s: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _row_underlying(row: dict) -> str:
    """Underlying ticker for a derivatives row, from the custom/trading symbol."""
    cs = (row.get("SEM_CUSTOM_SYMBOL") or "").strip()
    if cs:
        return cs.split(" ", 1)[0].upper()
    ts = (row.get("SEM_TRADING_SYMBOL") or "").strip()
    if "-" in ts:
        return ts.split("-", 1)[0].upper()
    return ts.upper()


def _strike_of(row: dict) -> float:
    try:
        return float(row.get("SEM_STRIKE_PRICE", "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def _nearest_expiry_row(rows: list[dict], today: date) -> str | None:
    dated = []
    for r in rows:
        d = _parse_expiry(r.get("SEM_EXPIRY_DATE", ""))
        if d and d >= today:
            dated.append((d, r.get("SEM_EXPIRY_DATE")))
    if not dated:
        return None
    dated.sort(key=lambda t: t[0])
    return dated[0][1]


# ── underlying → option-chain scrip/segment ──────────────────────────────────

def _underlying_scrip_seg(underlying: str):
    """(UnderlyingScrip:int, UnderlyingSeg:str) for the /optionchain endpoint."""
    key = _INDEX_ALIASES.get(underlying.upper().strip(), underlying.upper().strip())
    if key in DHAN_INDEX_UNDERLYINGS:
        return DHAN_INDEX_UNDERLYINGS[key]
    # Equity underlying: use its cash security id with NSE_FNO segment.
    _load_master()
    u = underlying.upper().strip()
    for r in _master:
        if (_exchange_segment(r.get("SEM_EXM_EXCH_ID", ""), r.get("SEM_SEGMENT", "")) == "NSE_EQ"
                and (r.get("SEM_TRADING_SYMBOL", "").upper() == u
                     or r.get("SEM_CUSTOM_SYMBOL", "").upper() == u)):
            sid = r.get("SEM_SMST_SECURITY_ID")
            if sid:
                try:
                    return int(sid), "NSE_FNO"
                except (TypeError, ValueError):
                    pass
    return None, None


# ── expiry list ──────────────────────────────────────────────────────────────

def _expiry_list(session, underlying: str) -> list[str]:
    key = _INDEX_ALIASES.get(underlying.upper().strip(), underlying.upper().strip())
    hit = _expiry_cache.get(key)
    if hit and (time.time() - hit[1]) < _EXPIRY_TTL_SEC:
        return hit[0]
    scrip, seg = _underlying_scrip_seg(underlying)
    if not scrip:
        return []
    try:
        resp = session.post("/optionchain/expirylist",
                            json={"UnderlyingScrip": scrip, "UnderlyingSeg": seg})
        exps = resp.get("data", []) if isinstance(resp, dict) else []
        _expiry_cache[key] = (exps, time.time())
        return exps
    except Exception as e:  # noqa: BLE001
        log.warning("Dhan expirylist failed for %s: %s", underlying, e)
        return []


def _nearest_expiry(session, underlying: str) -> str | None:
    today = date.today()
    dated = []
    for s in _expiry_list(session, underlying):
        d = _parse_expiry(s)
        if d and d >= today:
            dated.append((d, s))
    if not dated:
        return None
    dated.sort(key=lambda t: t[0])
    return dated[0][1]


# ── option chain (native greeks/IV/OI) ───────────────────────────────────────

def _r(v, nd):
    """Round to nd decimals, or None."""
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _compact_num(v):
    """Compact large counts for the LLM feed: 3786445→'3.79M', 1625→'1.6K'.

    Keeps the OI/volume signal while spending far fewer tokens than 7–9 digit ints.
    """
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    a = abs(n)
    if a >= 1e7:
        return f"{n / 1e6:.0f}M"
    if a >= 1e5:
        return f"{n / 1e6:.2f}M"
    if a >= 1e3:
        return f"{n / 1e3:.1f}K"
    return int(n) if n == int(n) else round(n, 1)


def get_option_chain(session, underlying: str, count: int = 4) -> dict:
    """Nearest-expiry chain around ATM, compacted for the LLM feed.

    Returns: {spot, expiry, pcr_oi, strikes: [{strike, option_type, premium,
              delta, iv, oi, volume}, ...]}.  pcr_oi = total PUT OI / total CALL
              OI across the FULL chain. Internal scrip_code and the
              theta/gamma/vega greeks are dropped to keep the prompt lean.
    """
    if session is None:
        return {}
    exp = _nearest_expiry(session, underlying)
    if not exp:
        return {}
    key = (_INDEX_ALIASES.get(underlying.upper().strip(), underlying.upper().strip()), exp)
    hit = _chain_cache.get(key)
    if hit and (time.time() - hit[1]) < _CHAIN_TTL_SEC:
        return _trim_chain(hit[0], count)

    scrip, seg = _underlying_scrip_seg(underlying)
    if not scrip:
        return {}
    try:
        resp = session.post("/optionchain", json={
            "UnderlyingScrip": scrip, "UnderlyingSeg": seg, "Expiry": exp})
    except Exception as e:  # noqa: BLE001
        log.warning("Dhan optionchain failed for %s %s: %s", underlying, exp, e)
        return {}

    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    spot = data.get("last_price")
    oc = data.get("oc", {}) or {}

    rows: list[dict] = []
    ce_oi = pe_oi = 0.0
    for strike_str, legs in oc.items():
        try:
            strike = float(strike_str)
        except (TypeError, ValueError):
            continue
        for ot_key, ot in (("ce", "CE"), ("pe", "PE")):
            leg = (legs or {}).get(ot_key)
            if not leg:
                continue
            oi = leg.get("oi")
            try:
                if ot == "CE":
                    ce_oi += float(oi or 0)
                else:
                    pe_oi += float(oi or 0)
            except (TypeError, ValueError):
                pass
            rows.append({
                "strike": strike,
                "option_type": ot,
                "premium": _r(leg.get("last_price"), 2),
                "delta": _r((leg.get("greeks") or {}).get("delta"), 2),
                "iv": _r(leg.get("implied_volatility"), 1),
                "oi": _compact_num(leg.get("oi")),
                "volume": _compact_num(leg.get("volume")),
            })

    full = {
        "spot": _r(spot, 2),
        "expiry": exp,
        "pcr_oi": round(pe_oi / ce_oi, 2) if ce_oi else None,
        "strikes": rows,
    }
    _chain_cache[key] = (full, time.time())
    return _trim_chain(full, count)


def _trim_chain(chain: dict, count: int) -> dict:
    """Trim a cached chain dict to ATM±count strikes so the feed stays bounded."""
    rows = (chain or {}).get("strikes") or []
    if not rows:
        return chain or {}
    strikes = sorted({r["strike"] for r in rows if r["strike"] > 0})
    ref = chain.get("spot") or (strikes[len(strikes) // 2] if strikes else None)
    wanted = set(strikes)
    if ref and strikes:
        atm = min(strikes, key=lambda s: abs(s - ref))
        idx = strikes.index(atm)
        lo, hi = max(0, idx - count), idx + count + 1
        wanted = set(strikes[lo:hi])
    trimmed = sorted((r for r in rows if r["strike"] in wanted),
                     key=lambda c: (c["strike"], c["option_type"]))
    return {"spot": chain.get("spot"), "expiry": chain.get("expiry"),
            "pcr_oi": chain.get("pcr_oi"), "strikes": trimmed}


# ── quotes (LTP) ──────────────────────────────────────────────────────────────

def _marketfeed_ltp(session, grouped: dict) -> dict | None:
    """POST /marketfeed/ltp under a process-wide 1 req/sec throttle, retrying 429.

    Holding the lock across the throttle-sleep + POST serialises every quote call
    in the process to ≤1/sec, which is what Dhan enforces.
    """
    global _ltp_last_ts
    for attempt in range(3):
        try:
            with _ltp_lock:
                wait = _LTP_MIN_INTERVAL - (time.time() - _ltp_last_ts)
                if wait > 0:
                    time.sleep(wait)
                resp = session.post("/marketfeed/ltp", json=grouped)
                _ltp_last_ts = time.time()
            return resp
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) == 429 and attempt < 2:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise
    return None


def get_ltp(session, scrip_codes: list[str]) -> dict[str, float]:
    """Batched LTP for "<SEG>:<id>" scrip-codes → {scrip_code: last_price}."""
    out: dict[str, float] = {}
    if session is None:
        return out
    uniq = [s for s in dict.fromkeys(scrip_codes) if s]
    for i in range(0, len(uniq), _LTP_BATCH):
        batch = uniq[i:i + _LTP_BATCH]
        grouped: dict[str, list[int]] = {}
        back: dict[tuple[str, str], str] = {}
        for code in batch:
            try:
                seg, sid = code.split(":", 1)
                grouped.setdefault(seg, []).append(int(sid))
                back[(seg, str(int(sid)))] = code
            except (ValueError, TypeError):
                continue
        if not grouped:
            continue
        try:
            resp = _marketfeed_ltp(session, grouped)
        except Exception as e:  # noqa: BLE001
            log.warning("Dhan LTP fetch failed for %s codes: %s", len(batch), e)
            continue
        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        for seg, byid in (data or {}).items():
            for sid, payload in (byid or {}).items():
                price = payload.get("last_price") if isinstance(payload, dict) else payload
                code = back.get((seg, str(sid)))
                if code and price is not None:
                    try:
                        out[code] = float(price)
                    except (TypeError, ValueError):
                        pass
    return out


# ── resolution (call → scrip-code) ───────────────────────────────────────────

def _index_scrip(underlying: str) -> str | None:
    key = _INDEX_ALIASES.get(underlying.upper().strip(), underlying.upper().strip())
    seg_id = DHAN_INDEX_UNDERLYINGS.get(key)
    return f"{seg_id[1]}:{seg_id[0]}" if seg_id else None


def _equity_scrip(underlying: str) -> str | None:
    _load_master()
    u = underlying.upper().strip()
    for r in _master:
        if _exchange_segment(r.get("SEM_EXM_EXCH_ID", ""), r.get("SEM_SEGMENT", "")) in ("NSE_EQ", "BSE_EQ"):
            if r.get("SEM_TRADING_SYMBOL", "").upper() == u or r.get("SEM_CUSTOM_SYMBOL", "").upper() == u:
                return _scrip_code(r)
    return None


def _future_scrip(underlying: str) -> str | None:
    _load_master()
    u = underlying.upper().strip()
    rows = [r for r in _master
            if _row_underlying(r) == u
            and "FUT" in (r.get("SEM_INSTRUMENT_NAME", "") or "").upper()]
    if not rows:
        return None
    exp = _nearest_expiry_row(rows, date.today())
    for r in rows:
        if r.get("SEM_EXPIRY_DATE") == exp:
            return _scrip_code(r)
    return _scrip_code(rows[0])


def _option_scrip(underlying: str, strike: float, opt_type: str, expiry: str | None = None) -> str | None:
    _load_master()
    u = underlying.upper().strip()
    ot = opt_type.upper().strip()
    rows = [r for r in _master
            if _row_underlying(r) == u and (r.get("SEM_OPTION_TYPE", "").upper() == ot)]
    if not rows:
        return None
    exp = expiry or _nearest_expiry_row(rows, date.today())

    def _match(r):
        return abs(_strike_of(r) - strike) < 0.5

    cand = [r for r in rows if r.get("SEM_EXPIRY_DATE") == exp and _match(r)]
    if not cand:
        cand = [r for r in rows if _match(r)]
    return _scrip_code(cand[0]) if cand else None


def resolve_scrip_for_call(call: dict) -> str | None:
    """Map an advisory call to a Dhan "<SEG>:<id>" scrip-code, or None."""
    cat = call.get("category")
    underlying = str(call.get("underlying") or "").strip()
    instrument = str(call.get("instrument") or "").strip()
    if not underlying:
        return None

    if cat == "index_option":
        m = _OPTION_RE.search(instrument)
        if not m:
            return None
        return _option_scrip(underlying, float(m.group(1)), m.group(2))
    if cat == "futures":
        return _future_scrip(underlying) or _index_scrip(underlying) or _equity_scrip(underlying)
    if cat == "equity":
        return _equity_scrip(underlying) or _index_scrip(underlying)
    return None  # commodity not wired yet


# ── snapshot / spots / lot size / expiry / price lookup ──────────────────────

def get_index_spots(session) -> dict[str, float]:
    """Live spot for NIFTY / BANKNIFTY / SENSEX / INDIA VIX (for triggers)."""
    if session is None:
        return {}
    codes = {}
    for key in ("NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX"):
        c = _index_scrip(key)
        if c:
            codes[key.lower()] = c
    if not codes:
        return {}
    prices = get_ltp(session, list(codes.values()))
    return {label: prices[c] for label, c in codes.items() if c in prices}


def get_market_snapshot(session) -> dict:
    """Index spot levels (Dhan) + global macro cues + technicals for grounding."""
    snap: dict = {}
    snap.update(get_index_spots(session))

    try:
        snap.update(fetch_global_data())  # crude_brent, usd_inr, dxy
    except Exception as e:  # noqa: BLE001
        log.warning("Global data fetch failed: %s", e)

    try:
        from core.technicals import get_technicals, get_intraday_technicals
        tech = get_technicals(["NIFTY", "BANKNIFTY", "SENSEX"])
        if tech:
            snap["technicals"] = tech
        intraday = get_intraday_technicals(["NIFTY", "BANKNIFTY", "SENSEX"])
        if intraday:
            snap["intraday"] = intraday
    except Exception as e:  # noqa: BLE001
        log.warning("Technicals fetch failed: %s", e)

    log.debug("Dhan market snapshot: %s", snap)
    return snap


def get_lot_size(session, underlying: str) -> int | None:
    """Lot size (SEM_LOT_UNITS) for an F&O underlying; fallback to PAPER_LOT_SIZES."""
    u = underlying.upper().strip()
    try:
        _load_master()
        for r in _master:
            if _row_underlying(r) == u and (r.get("SEM_OPTION_TYPE") or r.get("SEM_INSTRUMENT_NAME")):
                lot = r.get("SEM_LOT_UNITS")
                if lot:
                    try:
                        val = int(float(lot))
                        if val > 0:
                            return val
                    except (TypeError, ValueError):
                        pass
    except Exception as e:  # noqa: BLE001
        log.warning("Dhan lot-size lookup failed for %s: %s", u, e)
    return PAPER_LOT_SIZES.get(u)


def option_expiry_for(session, underlying: str, instrument: str) -> date | None:
    """Nearest-expiry DATE for the option in `instrument` (e.g. 'NIFTY 23400 PE')."""
    m = _OPTION_RE.search(instrument or "")
    if not m:
        return None
    strike, ot = float(m.group(1)), m.group(2).upper()
    u = (underlying or "").upper().strip()
    _load_master()
    rows = [r for r in _master
            if _row_underlying(r) == u and (r.get("SEM_OPTION_TYPE", "").upper() == ot)
            and abs(_strike_of(r) - strike) < 0.5]
    if not rows:
        rows = [r for r in _master
                if _row_underlying(r) == u and (r.get("SEM_OPTION_TYPE", "").upper() == ot)]
    exp = _nearest_expiry_row(rows, date.today()) if rows else None
    return _parse_expiry(exp) if exp else None


def get_positions(session) -> list[dict]:
    """Live broker positions (raw rows) for the read-only portfolio view.

    GET /v2/positions returns a JSON array of position objects (camelCase fields
    like netQty, tradingSymbol, buyAvg, realizedProfit). Field names are
    normalised downstream by the Telegram view's alias-tolerant parser.
    """
    if session is None:
        return []
    resp = session.get("/positions")
    if isinstance(resp, list):
        return resp
    data = resp.get("data", []) if isinstance(resp, dict) else []
    return data if isinstance(data, list) else []


def make_price_lookup(session):
    """Return `price_lookup(call) -> float | None` backed by Dhan LTP."""
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
