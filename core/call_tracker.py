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

from datetime import datetime
from typing import Callable

from config.logger import get_logger
from data.advisory_store import get_active_calls, update_call_status
from core.tape_filter import is_invalidated

log = get_logger("call_tracker")

PriceLookup = Callable[[dict], float | None]


def _pct(action: str, entry: float, exit_price: float) -> float:
    """Return signed return % for a closed call."""
    if entry == 0:
        return 0.0
    if action == "BUY":
        return round((exit_price - entry) / entry * 100, 2)
    return round((entry - exit_price) / entry * 100, 2)  # SELL


def _in_entry_zone(action: str, price: float, call: dict) -> bool:
    """Has price reached the recommended entry zone?"""
    emin = call.get("entry_min")
    emax = call.get("entry_max")
    if emin is not None and emax is not None:
        return float(emin) <= price <= float(emax)
    # No zone given → consider entry triggered once price crosses entry_price
    entry = float(call["entry_price"])
    if action == "BUY":
        return price <= entry  # buy on dip to entry or better
    return price >= entry      # sell on rise to entry or better


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
            # Trail the stop-loss to breakeven (entry) the moment T1 is hit, so the
            # remaining position can only exit at profit (T2) or flat (breakeven) —
            # never back at the original loss. Persisting it here means it works for
            # every call, including single-lot paper positions that can't be split.
            update_call_status(call["id"], "target1_hit", last_price=price,
                               result_pct=_pct(action, entry, t1), stop_loss=entry)
            return event("target1_hit", exit_price=t1)

    # 4) Entry trigger (informational)
    if not triggered and _in_entry_zone(action, price, call):
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
        # Expiry check first
        expires_at = call.get("expires_at")
        if expires_at and isinstance(expires_at, datetime) and now >= expires_at:
            last = call.get("last_price")
            result = None
            if last is not None and call.get("entry_price"):
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
