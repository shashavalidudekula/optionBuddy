"""
paper_trader.py — Shadow / paper-trading simulator.

Proves the engine's edge with ZERO real money before any live execution is ever
considered. It does NOT place real orders. Instead it maintains a simulated
account that "takes" qualifying calls and reacts to the SAME lifecycle events the
call tracker already emits:

    entry_triggered → open a risk-sized paper position
    target1_hit     → book a partial at T1
    target_hit      → close remaining at the final target
    sl_hit          → close at the stop-loss
    expired         → close at the last traded price

Sizing: risk PAPER_RISK_PCT (default 2%) of current equity to the stop, rounded
to whole lots. Guardrails: max concurrent positions, a daily loss limit, and the
confidence gate. Scope is limited to PAPER_CATEGORIES (index options to start).

Cash model (keeps equity internally consistent):
    BUY  entry: cash -= qty×entry      exit: cash += qty×exit   pnl = qty×(exit-entry)
    SELL entry: cash += qty×entry      exit: cash -= qty×exit   pnl = qty×(entry-exit)
"""

import math

from config.logger import get_logger
from config.settings import (
    MIN_CONFIDENCE,
    PAPER_CATEGORIES,
    PAPER_DAILY_LOSS_PCT,
    PAPER_MAX_OPEN,
    PAPER_PARTIAL_FRACTION,
    PAPER_RISK_PCT,
    PAPER_START_CAPITAL,
)
from core.indstocks_data import get_lot_size
from data.advisory_store import (
    book_paper_exit,
    compute_paper_equity,
    ensure_paper_account,
    get_open_paper_position_by_call,
    get_open_paper_positions,
    get_paper_account,
    get_paper_today_realized,
    open_paper_position,
    record_paper_equity,
    set_paper_position_last_price,
)

log = get_logger("paper_trader")


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pnl_cash(action: str, entry: float, exit_price: float, qty: int) -> tuple[float, float]:
    """Return (realized_pnl, cash_delta) for closing `qty` of a position."""
    if action == "BUY":
        return round((exit_price - entry) * qty, 2), round(qty * exit_price, 2)
    return round((entry - exit_price) * qty, 2), round(-qty * exit_price, 2)


class PaperTrader:
    """Simulated trading account driven by call-tracker lifecycle events."""

    def __init__(self, session=None):
        self.session = session
        self.account = ensure_paper_account(PAPER_START_CAPITAL)
        log.info("PaperTrader ready (capital ₹%.0f, risk %.0f%%, max %s open, scope %s)",
                 PAPER_START_CAPITAL, PAPER_RISK_PCT * 100, PAPER_MAX_OPEN,
                 ",".join(PAPER_CATEGORIES))

    # ── event routing ─────────────────────────────────────────────────────────

    def process_events(self, events: list[dict]) -> list[str]:
        """React to a batch of tracker events. Returns human-readable fill notes."""
        notes: list[str] = []
        for evt in events:
            call = evt.get("call") or {}
            if call.get("category") not in PAPER_CATEGORIES:
                continue
            et = evt.get("event_type")
            try:
                if et == "entry_triggered":
                    note = self._maybe_open(call, _f(evt.get("price")))
                elif et == "target1_hit":
                    note = self._book_partial(call, _f(call.get("target_1")))
                elif et == "target_hit":
                    note = self._close(call, _f(call.get("target_2")) or _f(call.get("target_1")), "exit")
                elif et == "sl_hit":
                    note = self._close(call, _f(call.get("stop_loss")), "stop")
                elif et == "expired":
                    note = self._close(call, _f(evt.get("price")), "expiry")
                else:
                    note = None
            except Exception as e:  # noqa: BLE001
                log.error("Paper event %s failed for call #%s: %s", et, call.get("id"), e)
                note = None
            if note:
                notes.append(note)
        return notes

    # ── entries ───────────────────────────────────────────────────────────────

    def _maybe_open(self, call: dict, price: float | None) -> str | None:
        call_id = call.get("id")
        if price is None or price <= 0:
            return None
        if get_open_paper_position_by_call(call_id):
            return None  # already holding this call

        # Guardrails ----------------------------------------------------------
        if len(get_open_paper_positions()) >= PAPER_MAX_OPEN:
            log.info("Paper skip (max %s open): %s", PAPER_MAX_OPEN, call.get("instrument"))
            return None
        loss_limit = -PAPER_DAILY_LOSS_PCT * PAPER_START_CAPITAL
        if get_paper_today_realized() <= loss_limit:
            log.info("Paper skip (daily loss limit ₹%.0f hit): %s", loss_limit, call.get("instrument"))
            return None
        if int(call.get("confidence") or 0) < MIN_CONFIDENCE:
            return None

        action = str(call.get("action", "")).upper()
        stop = _f(call.get("stop_loss"))
        if action not in ("BUY", "SELL") or stop is None:
            return None
        per_unit_risk = abs(price - stop)
        if per_unit_risk <= 0:
            return None

        underlying = str(call.get("underlying") or "").strip()
        lot_size = get_lot_size(self.session, underlying) if underlying else None
        if not lot_size:
            log.info("Paper skip (no lot size for %s): %s", underlying, call.get("instrument"))
            return None

        # Position size: risk PAPER_RISK_PCT of equity to the stop, whole lots ---
        equity = compute_paper_equity()
        risk_budget = PAPER_RISK_PCT * equity
        lots = math.floor(risk_budget / (per_unit_risk * lot_size))
        if lots < 1:
            log.info("Paper skip (1 lot risks > budget ₹%.0f): %s", risk_budget, call.get("instrument"))
            return None

        # Cash cap for long premium (BUY): can't spend more cash than we have.
        cash = float(get_paper_account()["cash"])
        if action == "BUY":
            affordable = math.floor(cash / (price * lot_size))
            lots = min(lots, affordable)
            if lots < 1:
                log.info("Paper skip (insufficient cash ₹%.0f): %s", cash, call.get("instrument"))
                return None

        qty = lots * lot_size
        cash_delta = -qty * price if action == "BUY" else qty * price
        open_paper_position(
            call_id=call_id,
            instrument=call.get("instrument"),
            underlying=underlying,
            category=call.get("category"),
            action=action,
            lot_size=lot_size,
            lots=lots,
            entry_price=round(price, 2),
            cash_delta=round(cash_delta, 2),
        )
        log.info("PAPER OPEN %s %s ×%s @ %.2f (%s lot)", action, call.get("instrument"),
                 qty, price, lots)

        t1, t2, sl = _f(call.get("target_1")), _f(call.get("target_2")), _f(call.get("stop_loss"))
        targets = " / ".join(f"₹{t:,.2f}" for t in (t1, t2) if t is not None)
        note = (f"📝 <b>PAPER OPEN</b> {action} <b>{call.get('instrument')}</b>\n"
                f"×{qty} ({lots} lot{'s' if lots > 1 else ''}) @ ₹{price:,.2f}")
        if targets:
            note += f"\n🎯 Target: {targets}"
        if sl is not None:
            note += f"\n🛑 Stop-loss: ₹{sl:,.2f}"
        return note

    # ── exits ─────────────────────────────────────────────────────────────────

    def _book_partial(self, call: dict, exit_price: float | None) -> str | None:
        pos = get_open_paper_position_by_call(call.get("id"))
        if not pos or exit_price is None:
            return None
        lot_size = int(pos["lot_size"])
        partial_lots = math.floor(int(pos["lots"]) * PAPER_PARTIAL_FRACTION)
        exit_qty = partial_lots * lot_size
        remaining = int(pos["remaining_qty"])
        # Need a non-trivial partial that still leaves something on the table.
        if exit_qty < lot_size or exit_qty >= remaining:
            return None

        action = str(pos["action"]).upper()
        entry = float(pos["entry_price"])
        realized, cash_delta = _pnl_cash(action, entry, exit_price, exit_qty)
        book_paper_exit(
            position_id=pos["id"], call_id=pos["call_id"], instrument=pos["instrument"],
            exit_qty=exit_qty, exit_price=round(exit_price, 2),
            realized_delta=realized, cash_delta=cash_delta, kind="partial", fully_closed=False,
        )
        log.info("PAPER PARTIAL %s ×%s @ %.2f (pnl %.0f)", pos["instrument"], exit_qty, exit_price, realized)
        emoji = "🟢" if realized >= 0 else "🔴"
        return (f"💰 <b>PAPER T1</b> booked {exit_qty} of <b>{pos['instrument']}</b> "
                f"@ ₹{exit_price:,.2f} {emoji} ₹{realized:,.0f}")

    def _close(self, call: dict, exit_price: float | None, kind: str) -> str | None:
        pos = get_open_paper_position_by_call(call.get("id"))
        if not pos:
            return None
        remaining = int(pos["remaining_qty"])
        if remaining <= 0:
            return None
        if exit_price is None:
            exit_price = float(pos["last_price"]) if pos["last_price"] is not None else float(pos["entry_price"])

        action = str(pos["action"]).upper()
        entry = float(pos["entry_price"])
        realized, cash_delta = _pnl_cash(action, entry, exit_price, remaining)
        book_paper_exit(
            position_id=pos["id"], call_id=pos["call_id"], instrument=pos["instrument"],
            exit_qty=remaining, exit_price=round(exit_price, 2),
            realized_delta=realized, cash_delta=cash_delta, kind=kind, fully_closed=True,
        )
        log.info("PAPER CLOSE (%s) %s ×%s @ %.2f (pnl %.0f)", kind, pos["instrument"],
                 remaining, exit_price, realized)
        label = {"exit": "🎯 PAPER TARGET", "stop": "🛑 PAPER STOP", "expiry": "⌛ PAPER EXPIRY"}.get(kind, "PAPER CLOSE")
        emoji = "✅" if realized >= 0 else "❌"
        return (f"{label} <b>{pos['instrument']}</b> ×{remaining} @ ₹{exit_price:,.2f} "
                f"{emoji} ₹{realized:,.0f}")

    # ── mark-to-market ─────────────────────────────────────────────────────────

    def mark_to_market(self, price_lookup) -> None:
        """Refresh open-position prices and record equity / drawdown."""
        for pos in get_open_paper_positions():
            mini_call = {
                "category": pos.get("category"),
                "underlying": pos.get("underlying"),
                "instrument": pos.get("instrument"),
            }
            try:
                price = price_lookup(mini_call)
            except Exception as e:  # noqa: BLE001
                log.debug("Paper MTM price lookup failed (%s): %s", pos.get("instrument"), e)
                price = None
            if price is not None:
                set_paper_position_last_price(pos["id"], round(float(price), 2))
        record_paper_equity(compute_paper_equity())
