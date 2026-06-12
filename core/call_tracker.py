"""
call_tracker.py — Lifecycle monitoring for published advisory calls.

Watches live prices for active calls and detects state transitions:
  active → entry_triggered → target1_hit → target_hit (T2)
                           ↘ sl_hit
  (any) → expired   (when expires_at passes)

Emits a list of "events" describing each transition so the caller can push
Telegram alerts (e.g. "🎯 Target 1 hit on RELIANCE — exit booked +2.4%").

The tracker is decoupled from the price source: the caller passes a
`price_lookup(call) -> float | None` callable. If a price is unavailable, the
call is left untouched (only expiry can still close it).
"""

from datetime import datetime, time as dtime
from typing import Callable

from config.logger import get_logger
from config.settings import T1_TRAIL_LOCK_FRACTION
from data.advisory_store import get_active_calls, update_call_status
from core.tape_filter import is_invalidated, confirm_call

log = get_logger("call_tracker")

PriceLookup = Callable[[dict], float | None]

_MARKET_CLOSE = dtime(15, 30)


def _contract_expired(call: dict, now: datetime) -> bool:
    """True when the option CONTRACT itself is dead (cash-settled 15:30 on expiry day).

    `expires_at` only covers the call's validity window — a swing-timeframe call
    on a weekly contract outlives the contract, leaving it 'waiting for trigger'
    on the dashboard days after the exchange has already settled it.
    """
    exp = call.get("option_expiry")
    if exp is None:
        return False
    if isinstance(exp, datetime):
        exp = exp.date()
    return exp < now.date() or (exp == now.date() and now.time() >= _MARKET_CLOSE)


def t1_locked_stop(action: str, entry: float, t1: float) -> float:
    """Trailed stop after T1: lock T1_TRAIL_LOCK_FRACTION of the entry→T1 move.

    At 0.75 a BUY stop sits 25% of the move below T1 — most of the T1 profit is
    kept, with enough room that noise around T1 doesn't shake out the runner.
    0 degrades to breakeven (the old behaviour); 1 is a stop exactly at T1.
    """
    frac = min(max(T1_TRAIL_LOCK_FRACTION, 0.0), 1.0)
    if action == "BUY":
        return round(entry + (t1 - entry) * frac, 2)
    return round(entry - (entry - t1) * frac, 2)


def _pct(action: str, entry: float, exit_price: float) -> float:
    """Return signed return % for a closed call."""
    if entry == 0:
        return 0.0
    if action == "BUY":
        return round((exit_price - entry) / entry * 100, 2)
    return round((entry - exit_price) / entry * 100, 2)  # SELL


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _in_entry_zone(action: str, price: float, call: dict) -> bool:
    """Has price reached an acceptable entry?

    BUY  → fill at or below entry_max — i.e. willing to pay up to your max. This
           catches BOTH dips and momentum/breakout fills, instead of requiring the
           premium to sit exactly inside [entry_min, entry_max] (which misses fast
           moves between polls — the cause of no opening-window trades).
    SELL → fill at or above entry_min.
    Falls back to entry_price when the band isn't provided.
    """
    emin, emax = _f(call.get("entry_min")), _f(call.get("entry_max"))
    entry = _f(call.get("entry_price"))
    if action == "BUY":
        cap = emax if emax is not None else entry
        return cap is not None and price <= cap
    floor_ = emin if emin is not None else entry
    return floor_ is not None and price >= floor_


def _evaluate_call(call: dict, price: float) -> dict | None:
    """Evaluate one call against current price. Returns an event dict or None.

    Event dict: {call_id, instrument, category, event_type, price, result_pct, call}
    event_type ∈ entry_triggered | target1_hit | target_hit | sl_hit
    """
    action = str(call["action"]).upper()
    entry = float(call["entry_price"])
    t1 = float(call["target_1"]) if call.get("target_1") is not None else None
    t2 = float(call["target_2"]) if call.get("target_2") is not None else None
    sl = float(call["stop_loss"]) if call.get("stop_loss") is not None else None
    status = call["status"]
    triggered = bool(call.get("entry_triggered"))

    def event(event_type, exit_price=None):
        return {
            "call_id": call["id"],
            "instrument": call["instrument"],
            "category": call["category"],
            "event_type": event_type,
            "price": price,
            "result_pct": _pct(action, entry, exit_price) if exit_price is not None else None,
            "call": call,
        }

    # 1) Stop-loss takes priority (risk-first)
    if sl is not None:
        if (action == "BUY" and price <= sl) or (action == "SELL" and price >= sl):
            update_call_status(call["id"], "sl_hit", last_price=price,
                               result_pct=_pct(action, entry, sl))
            return event("sl_hit", exit_price=sl)

    # 2) Final target (T2) → close
    if t2 is not None:
        if (action == "BUY" and price >= t2) or (action == "SELL" and price <= t2):
            update_call_status(call["id"], "target_hit", last_price=price,
                               result_pct=_pct(action, entry, t2))
            return event("target_hit", exit_price=t2)

    # 3) First target (T1) → partial, keep tracking toward T2
    if t1 is not None and status != "target1_hit":
        if (action == "BUY" and price >= t1) or (action == "SELL" and price <= t1):
            # If there is no T2, T1 closes the call.
            if t2 is None:
                update_call_status(call["id"], "target_hit", last_price=price,
                                   result_pct=_pct(action, entry, t1))
                return event("target_hit", exit_price=t1)
            # Trail the stop-loss to just below T1 the moment T1 is hit, locking
            # most of the T1 profit — breakeven alone let the runner ride a reversal
            # all the way back to flat. Persisting it here means it works for every
            # call, including single-lot paper positions that can't be split.
            update_call_status(call["id"], "target1_hit", last_price=price,
                               result_pct=_pct(action, entry, t1),
                               stop_loss=t1_locked_stop(action, entry, t1))
            return event("target1_hit", exit_price=t1)

    # 4) Entry trigger — re-confirm against the tape AT THE MOMENT the premium
    #    reaches the entry zone, not just at issue time. A BUY CE issued in an
    #    uptrend often only "comes back down" to its entry price because the
    #    trend reversed — filling it then buys a call into a falling market.
    if not triggered and _in_entry_zone(action, price, call):
        allow, why = confirm_call(call)
        if not allow:
            update_call_status(call["id"], "closed", last_price=price)
            log.info("Call #%s CANCELLED at entry @ %.2f (%s) — stale setup: %s",
                     call["id"], price, call["instrument"], why)
            return event("cancelled")
        update_call_status(call["id"], status, last_price=price, entry_triggered=True)
        return event("entry_triggered")

    # 5) No transition — just record last price
    update_call_status(call["id"], status, last_price=price)
    return None


def track_active_calls(price_lookup: PriceLookup) -> list[dict]:
    """Run one tracking pass over all active calls.

    Args:
        price_lookup: callable(call_dict) -> current price (float) or None.

    Returns:
        List of event dicts for transitions that occurred this pass.
    """
    events: list[dict] = []
    now = datetime.now()

    for call in get_active_calls():
        # Expiry check first: the call's validity window OR the option contract
        # itself lapsing — whichever comes first kills the call.
        expires_at = call.get("expires_at")
        validity_over = bool(expires_at and isinstance(expires_at, datetime) and now >= expires_at)
        if validity_over or _contract_expired(call, now):
            last = call.get("last_price")
            result = None
            # Only a triggered call has an outcome to score — an untriggered one
            # simply lapsed and shouldn't be booked with a phantom result.
            if call.get("entry_triggered") and last is not None and call.get("entry_price"):
                result = _pct(str(call["action"]).upper(),
                              float(call["entry_price"]), float(last))
            update_call_status(call["id"], "expired", result_pct=result)
            events.append({
                "call_id": call["id"],
                "instrument": call["instrument"],
                "category": call["category"],
                "event_type": "expired",
                "price": last,
                "result_pct": result,
                "call": call,
            })
            continue

        try:
            price = price_lookup(call)
        except Exception as e:
            log.warning("Price lookup failed for call #%s (%s): %s",
                        call["id"], call["instrument"], e)
            price = None

        if price is None:
            continue

        # Layer 3 — underlying invalidation: if the index has decisively reversed
        # against this call's direction, cut it now at the current premium (better
        # than riding a bearish position down to its stop while the market rallies).
        try:
            cut, why = is_invalidated(call)
        except Exception as e:  # noqa: BLE001
            log.debug("Invalidation check failed for call #%s: %s", call["id"], e)
            cut = False
        if cut:
            result = _pct(str(call["action"]).upper(), float(call["entry_price"]), float(price))
            update_call_status(call["id"], "closed", last_price=float(price), result_pct=result)
            log.info("Call #%s INVALIDATED @ %.2f (%s) — %s",
                     call["id"], float(price), call["instrument"], why)
            events.append({
                "call_id": call["id"],
                "instrument": call["instrument"],
                "category": call["category"],
                "event_type": "invalidated",
                "price": float(price),
                "result_pct": result,
                "call": call,
            })
            continue

        evt = _evaluate_call(call, float(price))
        if evt:
            log.info("Call #%s %s @ %.2f (%s)", call["id"], evt["event_type"],
                     float(price), call["instrument"])
            events.append(evt)

    return events
