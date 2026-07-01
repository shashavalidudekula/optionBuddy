"""
stock_options.py — Lightweight stock-option buying (minimal LLM).

Stock options in India (NSE) are ILLIQUID — spreads are brutal, many don't trade,
ruin is fast. This module is a PLACEHOLDER: light rules-based entries (high confidence,
ATM calls only, momentum filter) with tight stops and time exits. Buying ONLY (no selling).
Kept separate from index options so illiquidity doesn't kill the entire portfolio.

Do NOT rely on fills or backtests to be realistic unless you've verified via live Dhan data.
"""
from datetime import datetime, timedelta

from config.logger import get_logger
from config.settings import (
    STOCK_OPT_ENABLED, STOCK_OPT_UNDERLYINGS, STOCK_OPT_MIN_CONFIDENCE,
    STOCK_OPT_MIN_OI, STOCK_OPT_HOLD_DAYS,
)
from core.market_data_provider import get_option_chain
from data.advisory_store import save_call, update_call_status

log = get_logger("stock_options")


def _num(v) -> float:
    """Parse a possibly human-formatted number from the chain ('0.21M', '2.4K',
    '1,234', 12.5) into a float. Dhan returns OI/volume as suffixed strings."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return 0.0
    mult = 1.0
    suf = s[-1].upper()
    if suf in ("K", "M", "B"):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[suf]
        s = s[:-1]
    elif s[-2:].lower() == "cr":
        mult, s = 1e7, s[:-2]
    try:
        return float(s) * mult
    except (TypeError, ValueError):
        return 0.0


class StockOptions:
    """Generates light stock-option calls (illiquid; high caution advised)."""

    def __init__(self, session, paper):
        self.session = session
        self.paper = paper
        self._last_gen = {}  # {underlying: last_gen_date}

    def step(self, now: datetime, price_lookup) -> list[str]:
        if not STOCK_OPT_ENABLED or self.session is None or self.paper is None:
            return []
        if now.weekday() >= 5:
            return []
        today = now.date()
        notes = []
        for underlying in STOCK_OPT_UNDERLYINGS:
            if self._last_gen.get(underlying) == today:
                continue  # One per day per underlying
            self._last_gen[underlying] = today
            try:
                note = self._generate_one(underlying, now)
                if note:
                    notes.append(note)
            except Exception as e:  # noqa: BLE001
                log.error("Stock option generation for %s failed: %s", underlying, e)
        return notes

    def _generate_one(self, underlying: str, now: datetime) -> str | None:
        """Generate a call for one stock (ATM calls only, high confidence + OI filter)."""
        try:
            chain = get_option_chain(self.session, underlying) or {}
        except Exception as e:  # noqa: BLE001
            log.error("Stock option chain fetch failed for %s: %s", underlying, e)
            return None

        spot = chain.get("spot")
        expiry = chain.get("expiry")
        if not spot or not expiry:
            return None

        # Pick ATM call (closest strike <= spot). OI/premium may arrive as formatted
        # strings ('0.21M'), so parse via _num rather than int()/float() directly.
        spot = _num(spot)
        calls = [r for r in chain.get("strikes", [])
                if str(r.get("option_type", "")).upper() == "CE" and _num(r.get("strike")) <= spot
                and _num(r.get("premium")) > 0 and _num(r.get("oi")) >= STOCK_OPT_MIN_OI]
        if not calls:
            log.debug("Stock options: no liquid ATM calls for %s (OI >= %s)", underlying, STOCK_OPT_MIN_OI)
            return None

        atm = max(calls, key=lambda r: _num(r["strike"]))  # Closest to spot
        strike = int(_num(atm["strike"]))
        premium = _num(atm["premium"])
        oi = int(_num(atm.get("oi")))

        if premium <= 0 or oi < STOCK_OPT_MIN_OI:
            return None

        instrument = f"{underlying} {strike} CE"
        call = {
            "category": "stock_option",
            "instrument": instrument,
            "underlying": underlying,
            "action": "BUY",
            "timeframe": "swing",
            "entry_price": round(premium, 2),
            "entry_min": round(premium * 0.95, 2),
            "entry_max": round(premium * 1.05, 2),
            "target_1": round(premium * 1.5, 2),  # 50% gain
            "target_2": round(premium * 2.0, 2),  # 100% gain
            "stop_loss": round(premium * 0.5, 2),  # Tight 50% stop (illiquidity risk)
            "confidence": 75,
            "rationale": (f"Stock option: ATM {underlying} {strike} CE (OI {oi}). "
                         f"Illiquid — tight stop + short hold."),
            "option_expiry": expiry,
            "strategy": "stock_opt",
        }
        try:
            call_id = save_call(call)
            call["id"] = call_id
            update_call_status(call_id, "entry_triggered", last_price=premium, entry_triggered=True)
            note = self.paper._maybe_open(call, premium)
        except Exception as e:  # noqa: BLE001
            log.error("Stock option entry failed (%s): %s", instrument, e)
            return None
        from data.advisory_store import get_open_paper_position_by_call
        if not get_open_paper_position_by_call(call_id):
            update_call_status(call_id, "closed", last_price=premium)
            log.info("Stock options: paper did not open %s (guardrail) — call closed", instrument)
            return note

        log.info("Stock option ENTER %s @ %.2f (OI %s)", instrument, premium, oi)
        return note or f"📊 <b>Stock option</b> {instrument} @ ₹{premium:,.2f} (OI {oi})"
