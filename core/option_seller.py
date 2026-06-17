"""
option_seller.py — deterministic option-selling strategy (NO LLM).

Generates short call / put positions with defined-risk spreads and naked variants.
Picks strikes by delta + IV + DTE; exits at 50% of credit, stops at 2× credit, or time.

Naked shorts have higher premium but unlimited loss; spreads limit loss to the width
minus the net credit. Both are paper-safe with the realistic margin + slippage model.
"""
from datetime import datetime, timedelta

from config.logger import get_logger
from config.settings import (
    SELLING_ENABLED, SELLING_UNDERLYINGS, SELLING_DELTA_TARGET,
    SELLING_SPREAD_WIDTH, SELLING_EXIT_TAKE_PCT, SELLING_EXIT_STOP_MULTIPLE,
    SELLING_HOLD_DAYS, SELLING_STRUCTURE,
)
from core.market_data_provider import get_index_spots, get_option_chain
from core.margin import spread_margin
from data.advisory_store import save_call, update_call_status

log = get_logger("option_seller")


def pick_strike_by_delta(chain: dict, opt_type: str, spot: float, target_delta: float = 0.25,
                         prefer_iv: bool = True) -> dict | None:
    """Pick the short strike closest to `target_delta` (e.g., 0.25 for 25-delta call).

    opt_type: 'CE' (call) or 'PE' (put).
    prefer_iv: if multiple strikes are ~equal delta, pick the one with highest IV.
    Returns the strike dict {strike, premium, delta, iv, ...} or None if not found.

    Matches on the ABSOLUTE delta: Dhan reports put deltas as negative, so comparing
    the raw delta to +target would pin the short to the deepest-OTM put (delta→0) at
    the bottom of the chain — leaving no strike below it for a protective wing.
    """
    legs = [r for r in chain.get("strikes", [])
            if str(r.get("option_type", "")).upper() == opt_type and r.get("premium")]
    if not legs:
        return None
    # Sort by |delta| distance to target, then by IV (descending) if prefer_iv.
    legs_sorted = sorted(legs,
                         key=lambda r: (abs(abs(float(r.get("delta") or 0)) - target_delta),
                                       -float(r.get("iv") or 0) if prefer_iv else 0))
    return legs_sorted[0] if legs_sorted else None


def pick_wing_strike(chain: dict, short_strike: int, opt_type: str, spread_width: int,
                     direction: str = "out") -> dict | None:
    """Pick a protective wing strike ~`spread_width` points further OTM than the short.

    Robust to sparse/edge chains: instead of requiring an EXACT strike match (which
    failed when a deep-OTM wing has tiny/None premium and got filtered out), pick the
    available same-type strike that is further OTM than the short and closest to the
    target distance. The actual width is recomputed by the caller from the chosen strike.
    """
    opt_type = str(opt_type).upper()
    target = short_strike + spread_width if opt_type == "CE" else short_strike - spread_width

    cands = []
    for r in chain.get("strikes", []):
        if str(r.get("option_type", "")).upper() != opt_type:
            continue
        try:
            k = int(float(r.get("strike", 0)))
        except (TypeError, ValueError):
            continue
        otm = (k > short_strike) if opt_type == "CE" else (k < short_strike)
        if not otm or r.get("premium") is None:  # allow 0-premium wings (deep OTM), skip only missing
            continue
        cands.append((abs(k - target), r))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])  # closest to the target width wins
    return cands[0][1]


class OptionSeller:
    """Generates deterministic short-selling calls. Step every poll; manages exits via tracker."""

    def __init__(self, session, paper):
        self.session = session
        self.paper = paper  # One PaperTrader instance per selling book (opt_sell_spread/naked)
        self._positions = {}  # {call_id: {underlying, short_strike, wing_strike, entry_price, credit, ...}}
        self._last_gen = None  # throttle one trade per underlying per day

    def step(self, now: datetime, price_lookup) -> list[str]:
        if not SELLING_ENABLED or self.session is None or self.paper is None:
            return []
        if now.weekday() >= 5:
            return []
        today = now.date()
        if self._last_gen != today:
            self._last_gen = today
            return self._generate_daily(now)
        return []

    def _generate_daily(self, now: datetime) -> list[str]:
        """Generate one sell call per underlying (spreads by default; naked if configured)."""
        notes = []
        for underlying in SELLING_UNDERLYINGS:
            try:
                note = self._generate_one(underlying, now)
                if note:
                    notes.append(note)
            except Exception as e:  # noqa: BLE001
                log.error("Selling generation for %s failed: %s", underlying, e)
        return notes

    def _generate_one(self, underlying: str, now: datetime) -> str | None:
        """Generate a sell call for one underlying. Returns Telegram note or None."""
        try:
            # Wide window (ATM±35): the default ATM±4 is far too narrow for a delta-
            # based spread — the ~0.25-delta short sits well OTM and needs strikes
            # BELOW/ABOVE it for the protective wing. 35 strikes covers it comfortably.
            chain = get_option_chain(self.session, underlying, count=35) or {}
        except Exception as e:  # noqa: BLE001
            log.error("Seller option-chain fetch failed for %s: %s", underlying, e)
            return None

        spot = chain.get("spot")
        expiry = chain.get("expiry")
        if not spot or not expiry:
            log.warning("Seller: no spot/expiry for %s", underlying)
            return None

        # Decide sell side: pick a call or put based on IV / regime. For now, alternate or
        # pick the one with higher IV (volatility-preference variant). Let's start simple:
        # pick call if IV is higher, else put.
        call_iv = 0.0
        put_iv = 0.0
        for r in chain.get("strikes", []):
            if str(r.get("option_type", "")).upper() == "CE":
                call_iv = max(call_iv, float(r.get("iv") or 0))
            elif str(r.get("option_type", "")).upper() == "PE":
                put_iv = max(put_iv, float(r.get("iv") or 0))
        opt_type = "CE" if call_iv >= put_iv else "PE"

        # Pick short strike by delta.
        short_leg = pick_strike_by_delta(chain, opt_type, spot, target_delta=SELLING_DELTA_TARGET)
        if not short_leg:
            log.warning("Seller: no %s strike with delta ~%.2f for %s", opt_type, SELLING_DELTA_TARGET, underlying)
            return None

        short_strike = int(short_leg["strike"])
        short_premium = float(short_leg["premium"])
        short_delta = float(short_leg.get("delta") or 0)

        if SELLING_STRUCTURE.lower() == "spread":
            return self._enter_spread(underlying, opt_type, short_strike, short_premium,
                                     short_delta, chain, expiry, now)
        else:  # naked
            return self._enter_naked(underlying, opt_type, short_strike, short_premium,
                                    short_delta, chain, expiry, now)

    def _enter_spread(self, underlying: str, opt_type: str, short_strike: int,
                     short_premium: float, short_delta: float, chain: dict,
                     expiry, now: datetime) -> str | None:
        """Enter a defined-risk credit spread: sell short + buy protective wing."""
        wing = pick_wing_strike(chain, short_strike, opt_type, SELLING_SPREAD_WIDTH)
        if not wing:
            log.warning("Seller: no wing strike %d %d %s for %s", short_strike,
                       SELLING_SPREAD_WIDTH, opt_type, underlying)
            return None

        wing_strike = int(float(wing["strike"]))
        wing_premium = float(wing.get("premium") or 0.0)
        net_credit = short_premium - wing_premium
        width = abs(short_strike - wing_strike)  # ACTUAL width of the chosen wing

        if net_credit <= 0 or width <= 0:
            log.info("Seller: spread %s %d/%d no credit/width (credit %.2f, width %d) — skipping",
                    opt_type, short_strike, wing_strike, net_credit, width)
            return None

        lot_size = 75  # Standard index option lot size.
        max_loss = (width - net_credit) * lot_size
        qty = lot_size

        instrument = f"{underlying} {short_strike}/{wing_strike} {opt_type} spread"
        call = {
            "category": "index_option", "instrument": instrument, "underlying": underlying,
            "action": "SELL", "timeframe": "intraday",
            "entry_price": round(net_credit, 2),
            "entry_min": round(net_credit * 0.8, 2), "entry_max": round(net_credit * 1.2, 2),
            "target_1": round(net_credit * 0.5, 2),  # Take 50% of credit
            "target_2": round(0.0, 2),  # Expire worthless
            "stop_loss": round(net_credit * 2, 2),  # Stop at 2× the net credit (per unit)
            "confidence": 75,
            "rationale": (f"Defined-risk {opt_type} spread: sell {short_strike} / buy {wing_strike} "
                         f"(delta {short_delta:.2f}, width {width}). Max loss ₹{max_loss:.0f}."),
            "option_expiry": expiry,
            "strategy": "opt_sell_spread",
            # Embedded data so PaperTrader can fully automate the spread (both legs).
            "_spread_type": "defined_risk",
            "_short_strike": short_strike,
            "_wing_strike": wing_strike,
            "_short_premium": round(short_premium, 2),
            "_wing_premium": round(wing_premium, 2),
            "_width_points": width,
        }
        try:
            call_id = save_call(call)
            call["id"] = call_id
            update_call_status(call_id, "entry_triggered", last_price=net_credit, entry_triggered=True)
            note = self.paper._maybe_open(call, net_credit)
        except Exception as e:  # noqa: BLE001
            log.error("Seller spread entry failed (%s): %s", instrument, e)
            return None
        from data.advisory_store import get_open_paper_position_by_call
        if not get_open_paper_position_by_call(call_id):
            update_call_status(call_id, "closed", last_price=net_credit)
            log.info("Seller: paper did not open %s (guardrail) — call closed", instrument)
            return note

        self._positions[call_id] = {
            "underlying": underlying, "opt_type": opt_type,
            "short_strike": short_strike, "wing_strike": wing_strike,
            "entry_credit": net_credit, "max_loss": max_loss,
            "qty": qty, "expiry": expiry,
            "deadline": now + timedelta(days=SELLING_HOLD_DAYS),
        }
        log.info("Seller ENTER spread %s (short %d @ %.2f, wing %d @ %.2f, net credit %.2f, max loss ₹%.0f)",
                instrument, short_strike, short_premium, wing_strike, wing_premium, net_credit, max_loss)
        return note or f"📊 <b>Sell spread</b> {instrument} | net credit ₹{net_credit:,.2f}"

    def _enter_naked(self, underlying: str, opt_type: str, short_strike: int,
                    short_premium: float, short_delta: float, chain: dict,
                    expiry, now: datetime) -> str | None:
        """Enter a naked short: sell without a protective wing. Higher credit, unlimited loss."""
        lot_size = 75
        qty = lot_size

        instrument = f"{underlying} {short_strike} {opt_type} short"
        call = {
            "category": "index_option", "instrument": instrument, "underlying": underlying,
            "action": "SELL", "timeframe": "intraday",
            "entry_price": round(short_premium, 2),
            "entry_min": round(short_premium * 0.8, 2), "entry_max": round(short_premium * 1.2, 2),
            "target_1": round(short_premium * 0.5, 2),  # Take 50% of the premium
            "target_2": round(0.0, 2),  # Expire worthless
            "stop_loss": round(short_premium * 2, 2),  # Stop at 2× the premium received
            "confidence": 70,
            "rationale": (f"Naked {opt_type} short at {short_strike} (delta {short_delta:.2f}, iv high). "
                         f"Unlimited loss; stop at 2× premium."),
            "option_expiry": expiry,
            "strategy": "opt_sell_naked",
        }
        try:
            call_id = save_call(call)
            call["id"] = call_id
            update_call_status(call_id, "entry_triggered", last_price=short_premium, entry_triggered=True)
            note = self.paper._maybe_open(call, short_premium)
        except Exception as e:  # noqa: BLE001
            log.error("Seller naked entry failed (%s): %s", instrument, e)
            return None
        from data.advisory_store import get_open_paper_position_by_call
        if not get_open_paper_position_by_call(call_id):
            update_call_status(call_id, "closed", last_price=short_premium)
            log.info("Seller: paper did not open %s (guardrail) — call closed", instrument)
            return note

        self._positions[call_id] = {
            "underlying": underlying, "opt_type": opt_type,
            "short_strike": short_strike, "wing_strike": None,
            "entry_premium": short_premium, "max_loss": None,  # Unlimited
            "qty": qty, "expiry": expiry,
            "deadline": now + timedelta(days=SELLING_HOLD_DAYS),
        }
        log.info("Seller ENTER naked %s @ %.2f (premium, delta %.2f)", instrument, short_premium, short_delta)
        return note or f"📊 <b>Sell naked</b> {instrument} | premium ₹{short_premium:,.2f}"
