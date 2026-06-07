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

import json
import math
import os
from datetime import datetime

from config.logger import get_logger
from config.settings import (
    LOG_DIR,
    MIN_CONFIDENCE,
    PAPER_CATEGORIES,
    PAPER_DAILY_LOSS_PCT,
    PAPER_MAX_OPEN,
    PAPER_PARTIAL_FRACTION,
    PAPER_RISK_PCT,
    PAPER_START_CAPITAL,
)
from core.market_data_provider import get_lot_size, option_expiry_for
from core.execution import PaperBroker
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
    update_call_status,
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


# ── persistent daily history ───────────────────────────────────────────────────
# Closed trades are appended to a JSON-lines file on the host-mounted logs volume.
# It survives a Postgres reset (so the live account can start fresh each day while
# /paper <date> still shows past days' results).
_HISTORY_PATH = os.path.join(LOG_DIR, "paper_history.jsonl")


def _append_history(rec: dict) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(_HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("Paper history append failed: %s", e)


def read_paper_day(date_str: str) -> dict:
    """Aggregate the persisted closed trades for one date (YYYY-MM-DD)."""
    trades: list[dict] = []
    try:
        with open(_HISTORY_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("date") == date_str:
                    trades.append(rec)
    except FileNotFoundError:
        pass
    pnl = round(sum(float(t.get("pnl", 0) or 0) for t in trades), 2)
    wins = sum(1 for t in trades if float(t.get("pnl", 0) or 0) > 0)
    losses = sum(1 for t in trades if float(t.get("pnl", 0) or 0) < 0)
    return {
        "date": date_str,
        "trades": trades,
        "pnl": pnl,
        "count": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(100.0 * wins / (wins + losses), 1) if (wins + losses) else 0.0,
        "return_pct": round(pnl / PAPER_START_CAPITAL * 100, 2) if PAPER_START_CAPITAL else 0.0,
    }


class PaperTrader:
    """Simulated trading account driven by call-tracker lifecycle events."""

    def __init__(self, session=None, broker=None):
        self.session = session
        # Broker is the real-order hook; PaperBroker is a no-op (simulation only).
        # In live mode main passes a DhanBroker (still double-guarded).
        self.broker = broker or PaperBroker()
        self.account = ensure_paper_account(PAPER_START_CAPITAL)
        log.info("PaperTrader ready (capital ₹%.0f, risk %.0f%%, max %s open, scope %s, broker %s)",
                 PAPER_START_CAPITAL, PAPER_RISK_PCT * 100, PAPER_MAX_OPEN,
                 ",".join(PAPER_CATEGORIES), type(self.broker).__name__)

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
                elif et == "invalidated":
                    # Layer 3: underlying reversed against us — cut at current premium.
                    note = self._close(call, _f(evt.get("price")), "invalidated")
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
        try:  # real-order hook (no-op in paper; double-guarded in live)
            self.broker.place_entry(call, action, qty, price)
        except Exception as e:  # noqa: BLE001
            log.error("Broker place_entry failed (%s): %s", call.get("instrument"), e)

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

    @staticmethod
    def _t1_exit_fraction(category: str) -> float:
        """Return the fraction of position to exit at T1 based on category.

        Index options: exit 70% (keep 30% for T2)
        Stocks/Futures: exit 60% (keep 40% for T2)
        """
        if category == "index_option":
            return 0.7
        return 0.6

    def _book_partial(self, call: dict, exit_price: float | None) -> str | None:
        pos = get_open_paper_position_by_call(call.get("id"))
        if not pos or exit_price is None:
            return None
        lot_size = int(pos["lot_size"])
        category = pos.get("category", "")
        exit_fraction = self._t1_exit_fraction(category)
        partial_lots = math.floor(int(pos["lots"]) * exit_fraction)
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

        # Trail stop-loss to breakeven (entry price) after T1 is booked
        update_call_status(pos["call_id"], call.get("status", "entry_triggered"),
                          stop_loss=entry)

        log.info("PAPER PARTIAL %s ×%s @ %.2f (pnl %.0f) | SL trailed to breakeven ₹%.2f",
                 pos["instrument"], exit_qty, exit_price, realized, entry)
        try:  # real-order hook (no-op in paper; double-guarded in live)
            self.broker.place_exit(call, action, exit_qty, exit_price, "partial")
        except Exception as e:  # noqa: BLE001
            log.error("Broker place_exit (partial) failed (%s): %s", pos["instrument"], e)
        self._log_trade(pos, exit_qty, exit_price, realized, "partial")
        emoji = "🟢" if realized >= 0 else "🔴"
        return (f"💰 <b>PAPER T1</b> booked {exit_qty} of <b>{pos['instrument']}</b> "
                f"@ ₹{exit_price:,.2f} {emoji} ₹{realized:,.0f}\n"
                f"🛑 SL trailed to breakeven (₹{entry:,.2f})")

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
        try:  # real-order hook (no-op in paper; double-guarded in live)
            self.broker.place_exit(
                {"category": pos.get("category"), "underlying": pos.get("underlying"),
                 "instrument": pos.get("instrument")},
                action, remaining, exit_price, kind)
        except Exception as e:  # noqa: BLE001
            log.error("Broker place_exit (%s) failed (%s): %s", kind, pos["instrument"], e)
        self._log_trade(pos, remaining, exit_price, realized, kind)
        label = {"exit": "🎯 PAPER TARGET", "stop": "🛑 PAPER STOP", "expiry": "⌛ PAPER EXPIRY",
                 "invalidated": "🔄 PAPER CUT (trend reversed)"}.get(kind, "PAPER CLOSE")
        emoji = "✅" if realized >= 0 else "❌"
        return (f"{label} <b>{pos['instrument']}</b> ×{remaining} @ ₹{exit_price:,.2f} "
                f"{emoji} ₹{realized:,.0f}")

    @staticmethod
    def _log_trade(pos: dict, qty, exit_price, realized, kind: str) -> None:
        """Append a closed-trade record to the durable daily history."""
        now = datetime.now()
        _append_history({
            "date": now.strftime("%Y-%m-%d"),
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "instrument": pos.get("instrument"),
            "underlying": pos.get("underlying"),
            "category": pos.get("category"),
            "action": str(pos.get("action")).upper(),
            "qty": int(qty),
            "entry": float(pos.get("entry_price")),
            "exit": round(float(exit_price), 2),
            "pnl": realized,
            "kind": kind,
        })

    # ── expiry settlement ───────────────────────────────────────────────────────

    def settle_expiry(self, price_lookup, today) -> list[str]:
        """Close any open position whose option expires on/before `today`.

        Options are cash-settled at expiry, so at the close of the expiry day we
        settle the remaining lots at the last traded premium (best available proxy
        when the live feed is already down after hours).
        """
        from data.advisory_store import get_call_levels

        notes: list[str] = []
        open_pos = get_open_paper_positions()
        if not open_pos:
            return notes
        levels = get_call_levels([p.get("call_id") for p in open_pos])
        for pos in open_pos:
            cid = pos.get("call_id")
            exp = (levels.get(cid) or {}).get("option_expiry")
            if exp is None:  # older calls: resolve the contract expiry from the master
                try:
                    exp = option_expiry_for(self.session, pos.get("underlying", ""), pos.get("instrument", ""))
                except Exception:  # noqa: BLE001
                    exp = None
            if exp is None or exp > today:
                continue
            mini = {"category": pos.get("category"), "underlying": pos.get("underlying"),
                    "instrument": pos.get("instrument")}
            price = None
            try:
                price = price_lookup(mini)
            except Exception:  # noqa: BLE001
                price = None
            if price is None:
                price = float(pos["last_price"]) if pos["last_price"] is not None else float(pos["entry_price"])
            note = self._close({"id": cid}, float(price), "expiry")
            if note:
                log.info("PAPER EXPIRY SETTLE %s (expiry %s) @ %.2f", pos.get("instrument"), exp, float(price))
                notes.append(note)
        return notes

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
