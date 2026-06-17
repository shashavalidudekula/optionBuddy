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
from datetime import datetime

from config.logger import get_logger
from config.settings import (
    MIN_CONFIDENCE,
    PAPER_CATEGORIES,
    PAPER_COST_PER_TRADE,
    PAPER_DAILY_LOSS_PCT,
    PAPER_MAX_LOSS_PER_TRADE,
    PAPER_RISK_CAP_ENABLED,
    PAPER_MAX_LOTS,
    PAPER_MAX_OPEN,
    PAPER_MIN_LOTS,
    PAPER_PARTIAL_FRACTION,
    PAPER_RISK_PCT,
    PAPER_SLIPPAGE_PCT,
    PAPER_START_CAPITAL,
)
from core.market_data_provider import get_lot_size, option_expiry_for
from core.call_tracker import t1_locked_stop
from core.execution import PaperBroker
from core.margin import naked_short_margin, spread_margin, max_affordable_lots
# Durable closed-trade ledger (survives capital resets). read_paper_day is
# re-exported here so existing imports (Telegram /paper) keep working.
from data.paper_history import append_history, read_paper_day  # noqa: F401
from data.advisory_store import (
    DEFAULT_STRATEGY,
    book_paper_exit,
    compute_paper_equity,
    ensure_paper_account,
    get_open_paper_position_by_call,
    get_open_paper_positions,
    get_paper_account,
    get_paper_today_realized,
    open_paper_position,
    record_paper_equity,
    set_call_paper_status,
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


def _slip_fill(price: float, side: str, slip: float | None = None) -> float:
    """Adverse slippage on a MARKET fill, as a fraction of the premium.

    Modelling zero slippage was the biggest source of paper-P&L optimism: option
    books are wide (worse near expiry), and stop/market exits gap past their
    trigger. `side='buy'` pays UP (a long entry, or buy-to-close a short);
    `side='sell'` receives LESS (sell-to-open / sell-to-close a long). Limit/target
    fills do NOT pass through here — a resting limit fills at its own level.

    `slip` overrides the per-book slippage fraction; None → the global default.
    """
    slip = PAPER_SLIPPAGE_PCT if slip is None else slip
    slip = max(slip, 0.0)
    if slip <= 0:
        return round(price, 2)
    return round(price * (1 + slip), 2) if side == "buy" else round(price * (1 - slip), 2)


def size_by_risk(*, equity: float, per_unit_risk: float, lot_size: int, risk_pct: float,
                 max_loss_per_trade: float, risk_cap_enabled: bool, min_lots: int,
                 max_lots: int) -> tuple[int, str | None]:
    """Risk-based lot sizing shared by long and short entries (pure / testable).

    Returns (lots, skip_reason). With the per-trade cap ON, risk the SMALLER of
    risk_pct×equity and max_loss_per_trade to the stop; if even one lot breaches it,
    return (0, 'risk_skip'). With the cap OFF, size by risk_pct floored at min_lots.
    Mirrors the original long sizing exactly, so the opt_buy book is unchanged.
    """
    one_lot_risk = per_unit_risk * lot_size
    if one_lot_risk <= 0:
        return 0, "bad_risk"
    if risk_cap_enabled:
        risk_cap = min(risk_pct * equity, max_loss_per_trade)
        risk_lots = math.floor(risk_cap / one_lot_risk)
        if risk_lots < 1:
            return 0, "risk_skip"
        lots = risk_lots
    else:
        risk_lots = math.floor(risk_pct * equity / one_lot_risk)
        lots = max(min_lots, risk_lots)
    if max_lots > 0:
        lots = min(lots, max_lots)
    return lots, None


class PaperTrader:
    """Simulated trading account driven by call-tracker lifecycle events."""

    def __init__(self, session=None, broker=None, *, strategy=DEFAULT_STRATEGY, capital=None,
                 categories=None, risk_pct=None, min_lots=None, max_lots=None, max_open=None,
                 daily_loss_pct=None, cost_per_trade=None, slippage_pct=None,
                 risk_cap_enabled=None, max_loss_per_trade=None, partial_fraction=None,
                 allow_buy=True, allow_sell=False):
        self.session = session
        # Broker is the real-order hook; PaperBroker is a no-op (simulation only).
        # In live mode main passes a DhanBroker (still double-guarded).
        self.broker = broker or PaperBroker()
        # One PaperTrader == one paper book. Defaults fall back to the global PAPER_*
        # settings so the existing single (opt_buy) instantiation is unchanged.
        self.strategy = strategy
        self.capital = PAPER_START_CAPITAL if capital is None else float(capital)
        self.categories = tuple(categories) if categories is not None else PAPER_CATEGORIES
        self.risk_pct = PAPER_RISK_PCT if risk_pct is None else risk_pct
        self.min_lots = PAPER_MIN_LOTS if min_lots is None else min_lots
        self.max_lots = PAPER_MAX_LOTS if max_lots is None else max_lots
        self.max_open = PAPER_MAX_OPEN if max_open is None else max_open
        self.daily_loss_pct = PAPER_DAILY_LOSS_PCT if daily_loss_pct is None else daily_loss_pct
        self.cost_per_trade = PAPER_COST_PER_TRADE if cost_per_trade is None else cost_per_trade
        self.slippage_pct = PAPER_SLIPPAGE_PCT if slippage_pct is None else slippage_pct
        self.risk_cap_enabled = PAPER_RISK_CAP_ENABLED if risk_cap_enabled is None else risk_cap_enabled
        self.max_loss_per_trade = PAPER_MAX_LOSS_PER_TRADE if max_loss_per_trade is None else max_loss_per_trade
        self.partial_fraction = PAPER_PARTIAL_FRACTION if partial_fraction is None else partial_fraction
        self.allow_buy = allow_buy
        self.allow_sell = allow_sell
        self.account = ensure_paper_account(self.capital, self.strategy)
        log.info("PaperTrader [%s] ready (capital ₹%.0f, risk %.0f%%, min %s lots, max open %s, "
                 "scope %s, sides %s, broker %s)",
                 self.strategy, self.capital, self.risk_pct * 100, self.min_lots,
                 self.max_open if self.max_open > 0 else "unlimited",
                 ",".join(self.categories),
                 "+".join([s for s, on in (("BUY", allow_buy), ("SELL", allow_sell)) if on]) or "none",
                 type(self.broker).__name__)

    # ── event routing ─────────────────────────────────────────────────────────

    def process_events(self, events: list[dict]) -> list[str]:
        """React to a batch of tracker events. Returns human-readable fill notes."""
        notes: list[str] = []
        for evt in events:
            call = evt.get("call") or {}
            # Route to the owning book: a call belongs to exactly one strategy, and
            # must also be in this book's category scope.
            if str(call.get("strategy") or DEFAULT_STRATEGY) != self.strategy:
                continue
            if call.get("category") not in self.categories:
                continue
            et = evt.get("event_type")
            try:
                if et == "entry_triggered":
                    note = self._maybe_open(call, _f(evt.get("price")))
                elif et == "target1_hit":
                    # T1 is a resting limit → fills at its level (no slippage).
                    note = self._book_partial(call, _f(call.get("target_1")), market=False)
                elif et == "time_partial":
                    # Profit stalled below T1 — bank a partial at the current premium (market fill).
                    note = self._book_partial(call, _f(evt.get("price")), market=True)
                elif et == "target_hit":
                    # T2 (or T1 when there is no T2) is a resting limit → fills at its level.
                    note = self._close(call, _f(call.get("target_2")) or _f(call.get("target_1")), "exit", market=False)
                elif et == "sl_hit":
                    # A stop is a MARKET exit. Book it at the premium ACTUALLY OBSERVED
                    # when the stop broke (already gapped past the stop level), not at the
                    # idealised stop — then slip it worse still inside _close. This was
                    # the single biggest source of paper-P&L optimism.
                    note = self._close(call, _f(evt.get("price")), "stop", market=True)
                elif et == "expired":
                    note = self._close(call, _f(evt.get("price")), "expiry", market=True)
                elif et == "invalidated":
                    # Layer 3: underlying reversed against us — cut at current premium (market).
                    note = self._close(call, _f(evt.get("price")), "invalidated", market=True)
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

        action = str(call.get("action", "")).upper()
        if action == "BUY" and not self.allow_buy:
            return None
        if action == "SELL" and not self.allow_sell:
            log.info("Paper [%s] skip (SELL not enabled): %s", self.strategy, call.get("instrument"))
            return None
        if action not in ("BUY", "SELL"):
            return None

        # Guardrails (side-agnostic) -----------------------------------------
        # max_open <= 0 → unlimited concurrent positions (capital/margin is the limit).
        if self.max_open > 0 and len(get_open_paper_positions(self.strategy)) >= self.max_open:
            log.info("Paper [%s] NOT EXECUTED (max %s open): %s", self.strategy, self.max_open, call.get("instrument"))
            set_call_paper_status(call_id, "capped")
            return (f"⏸️ <b>Not executed</b> · {call.get('instrument')}\n"
                    f"Max {self.max_open} positions open — capital tied up. Call still logged.")
        if self.daily_loss_pct > 0:
            loss_limit = -self.daily_loss_pct * self.capital
            if get_paper_today_realized(self.strategy) <= loss_limit:
                log.info("Paper [%s] NOT EXECUTED (daily loss limit): %s", self.strategy, call.get("instrument"))
                set_call_paper_status(call_id, "halted_daily_loss")
                return (f"⏸️ <b>Not executed</b> · {call.get('instrument')}\n"
                        f"Daily loss limit ₹{abs(loss_limit):,.0f} hit — execution halted today.")
        if int(call.get("confidence") or 0) < MIN_CONFIDENCE:
            return None

        stop = _f(call.get("stop_loss"))
        if stop is None:
            return None
        underlying = str(call.get("underlying") or "").strip()
        lot_size = get_lot_size(self.session, underlying) if underlying else None
        if not lot_size:
            log.info("Paper [%s] skip (no lot size for %s): %s", self.strategy, underlying, call.get("instrument"))
            return None

        if action == "BUY":
            return self._open_long(call, call_id, price, stop, underlying, lot_size)
        return self._open_short(call, call_id, price, stop, underlying, lot_size)

    def _open_long(self, call, call_id, price, stop, underlying, lot_size) -> str | None:
        """Buy to open: deploy premium up front (no margin). Identical to the
        original long path, now config-driven per book."""
        quote = price
        price = _slip_fill(price, "buy", self.slippage_pct)      # long entry crosses the spread
        stop_fill = _slip_fill(stop, "sell", self.slippage_pct)  # the stop exit slips against us too
        per_unit_risk = abs(price - stop_fill)
        if per_unit_risk <= 0:
            return None

        equity = compute_paper_equity(self.strategy)
        lots, skip = size_by_risk(
            equity=equity, per_unit_risk=per_unit_risk, lot_size=lot_size,
            risk_pct=self.risk_pct, max_loss_per_trade=self.max_loss_per_trade,
            risk_cap_enabled=self.risk_cap_enabled, min_lots=self.min_lots, max_lots=self.max_lots)
        if skip == "risk_skip":
            one_lot_risk = per_unit_risk * lot_size
            risk_cap = min(self.risk_pct * equity, self.max_loss_per_trade)
            log.info("Paper [%s] NOT EXECUTED (1 lot risks ₹%.0f > cap ₹%.0f): %s",
                     self.strategy, one_lot_risk, risk_cap, call.get("instrument"))
            set_call_paper_status(call_id, "risk_skip")
            return (f"⛔ <b>Not executed</b> · {call.get('instrument')}\n"
                    f"1 lot risks ₹{one_lot_risk:,.0f} (SL {per_unit_risk:,.0f} × {lot_size}) "
                    f"&gt; per-trade cap ₹{risk_cap:,.0f} — skipped to protect the daily limit.")
        if lots < 1:
            return None

        # Capital halt: a long deploys premium/price × qty up front.
        cash = float(get_paper_account(self.strategy)["cash"])
        cost_per_lot = price * lot_size
        affordable = math.floor(cash / cost_per_lot) if cost_per_lot > 0 else 0
        if affordable < 1:
            log.info("Paper [%s] NOT EXECUTED (capital exhausted: free ₹%.0f < 1-lot ₹%.0f): %s",
                     self.strategy, cash, cost_per_lot, call.get("instrument"))
            set_call_paper_status(call_id, "unfunded")
            return (f"⛔ <b>Not executed</b> · {call.get('instrument')}\n"
                    f"Capital exhausted — free ₹{cash:,.0f} &lt; 1-lot ₹{cost_per_lot:,.0f}. "
                    f"Call still logged & shown on the dashboard.")
        lots = min(lots, affordable)

        qty = lots * lot_size
        open_paper_position(
            call_id=call_id, instrument=call.get("instrument"), underlying=underlying,
            category=call.get("category"), action="BUY", lot_size=lot_size, lots=lots,
            entry_price=round(price, 2), cash_delta=round(-qty * price, 2), strategy=self.strategy,
        )
        set_call_paper_status(call_id, "executed")
        log.info("PAPER [%s] OPEN BUY %s ×%s @ %.2f (quote %.2f + slip, %s lot, deployed ₹%.0f)",
                 self.strategy, call.get("instrument"), qty, price, quote, lots, qty * price)
        self._safe_broker_entry(call, "BUY", qty, price)
        return self._open_note("BUY", call, qty, lots, price)

    def _open_short(self, call, call_id, price, stop, underlying, lot_size) -> str | None:
        """Sell to open: can be naked (single leg) or spread (both legs automated).
        Check if the call has spread metadata; if yes, open spread. Else, naked."""
        # Check for embedded spread data (set by option_seller.py for spreads).
        if call.get("_spread_type") == "defined_risk":
            return self._open_spread(call, call_id, underlying, lot_size)

        # Naked short: single leg, unlimited loss.
        quote = price
        price = _slip_fill(price, "sell", self.slippage_pct)    # sell-to-open receives LESS
        stop_fill = _slip_fill(stop, "buy", self.slippage_pct)  # buy-to-close slips UP
        per_unit_risk = abs(stop_fill - price)
        if per_unit_risk <= 0:
            return None

        equity = compute_paper_equity(self.strategy)
        lots, skip = size_by_risk(
            equity=equity, per_unit_risk=per_unit_risk, lot_size=lot_size,
            risk_pct=self.risk_pct, max_loss_per_trade=self.max_loss_per_trade,
            risk_cap_enabled=self.risk_cap_enabled, min_lots=self.min_lots, max_lots=self.max_lots)
        if skip == "risk_skip":
            one_lot_risk = per_unit_risk * lot_size
            risk_cap = min(self.risk_pct * equity, self.max_loss_per_trade)
            log.info("Paper [%s] NOT EXECUTED (1 lot risks ₹%.0f > cap ₹%.0f): %s",
                     self.strategy, one_lot_risk, risk_cap, call.get("instrument"))
            set_call_paper_status(call_id, "risk_skip")
            return (f"⛔ <b>Not executed</b> · {call.get('instrument')}\n"
                    f"1 lot (buy-back) risks ₹{one_lot_risk:,.0f} &gt; per-trade cap ₹{risk_cap:,.0f} — skipped.")
        if lots < 1:
            return None

        # Margin gate: a naked short blocks SPAN/exposure margin.
        acct = get_paper_account(self.strategy)
        free_margin = float(acct["cash"]) - float(acct.get("margin_used") or 0)
        affordable = max_affordable_lots(free_margin, underlying, structure="naked")
        if affordable < 1:
            per_lot = naked_short_margin(underlying, 1)
            log.info("Paper [%s] NOT EXECUTED (margin exhausted: free ₹%.0f < 1-lot ₹%.0f): %s",
                     self.strategy, free_margin, per_lot, call.get("instrument"))
            set_call_paper_status(call_id, "unfunded")
            return (f"⛔ <b>Not executed</b> · {call.get('instrument')}\n"
                    f"Margin exhausted — free ₹{free_margin:,.0f} &lt; 1-lot margin ₹{per_lot:,.0f}.")
        lots = min(lots, affordable)

        qty = lots * lot_size
        margin_blocked = naked_short_margin(underlying, lots)
        premium_received = round(qty * price, 2)
        open_paper_position(
            call_id=call_id, instrument=call.get("instrument"), underlying=underlying,
            category=call.get("category"), action="SELL", lot_size=lot_size, lots=lots,
            entry_price=round(price, 2), cash_delta=premium_received, strategy=self.strategy,
            premium_received=premium_received, margin_blocked=margin_blocked, structure="single",
        )
        set_call_paper_status(call_id, "executed")
        log.info("PAPER [%s] OPEN SELL %s ×%s @ %.2f (quote %.2f − slip, %s lot, recv ₹%.0f, margin ₹%.0f)",
                 self.strategy, call.get("instrument"), qty, price, quote, lots, premium_received, margin_blocked)
        self._safe_broker_entry(call, "SELL", qty, price)
        return self._open_note("SELL", call, qty, lots, price, margin=margin_blocked)

    def _open_spread(self, call, call_id, underlying, lot_size) -> str | None:
        """Defined-risk spread: short leg + long protective wing (both legs automated)."""
        import json as _json
        short_strike = int(call.get("_short_strike", 0))
        wing_strike = int(call.get("_wing_strike", 0))
        short_premium = float(call.get("_short_premium", 0))
        wing_premium = float(call.get("_wing_premium", 0))
        width_points = int(call.get("_width_points", 0))

        if not (short_strike and wing_strike and short_premium > 0 and width_points > 0):
            log.warning("Paper [%s]: spread call missing critical fields — skipping", self.strategy)
            return None

        net_credit = short_premium - wing_premium
        if net_credit <= 0:
            log.info("Paper [%s]: spread %s has no net credit (%.2f − %.2f) — skipping",
                    self.strategy, call.get("instrument"), short_premium, wing_premium)
            set_call_paper_status(call_id, "unfunded")
            return None

        # Size by risk: per_unit_risk = width - credit (max loss per unit).
        per_unit_risk = width_points - net_credit
        equity = compute_paper_equity(self.strategy)
        lots, skip = size_by_risk(
            equity=equity, per_unit_risk=per_unit_risk, lot_size=lot_size,
            risk_pct=self.risk_pct, max_loss_per_trade=self.max_loss_per_trade,
            risk_cap_enabled=self.risk_cap_enabled, min_lots=self.min_lots, max_lots=self.max_lots)
        if skip or lots < 1:
            if skip:
                log.info("Paper [%s] NOT EXECUTED (spread risk cap): %s", self.strategy, call.get("instrument"))
            set_call_paper_status(call_id, "risk_skip" if skip else "unfunded")
            return None

        # Margin gate: spread margin = max loss = (width − credit) × qty.
        qty = lots * lot_size
        margin_required = spread_margin(width_points, net_credit, lot_size, lots)
        acct = get_paper_account(self.strategy)
        free_margin = float(acct["cash"]) - float(acct.get("margin_used") or 0)

        if free_margin < margin_required:
            log.info("Paper [%s] NOT EXECUTED (spread margin: free ₹%.0f < required ₹%.0f): %s",
                     self.strategy, free_margin, margin_required, call.get("instrument"))
            set_call_paper_status(call_id, "unfunded")
            return (f"⛔ <b>Not executed</b> · {call.get('instrument')}\n"
                    f"Margin exhausted — free ₹{free_margin:,.0f} &lt; spread margin ₹{margin_required:,.0f}.")

        # Open the spread as a single position with both legs' data (for exit atomicity).
        legs_data = {
            "short_strike": short_strike,
            "short_premium": short_premium,
            "wing_strike": wing_strike,
            "wing_premium": wing_premium,
            "width_points": width_points,
        }
        net_premium_received = round(net_credit * qty, 2)
        open_paper_position(
            call_id=call_id, instrument=call.get("instrument"), underlying=underlying,
            category=call.get("category"), action="SELL", lot_size=lot_size, lots=lots,
            entry_price=round(net_credit, 2),  # entry = net credit per unit
            cash_delta=net_premium_received,  # cash in = total net credit
            strategy=self.strategy,
            premium_received=net_premium_received,
            margin_blocked=round(margin_required, 2),
            structure="spread",
            legs=legs_data,
        )
        set_call_paper_status(call_id, "executed")
        log.info("PAPER [%s] OPEN SPREAD %s (×%s lots) | short %d @ %.2f, wing %d @ %.2f, "
                 "net credit %.2f/unit = ₹%.2f total, margin ₹%.0f",
                 self.strategy, call.get("instrument"), lots, short_strike, short_premium,
                 wing_strike, wing_premium, net_credit, net_premium_received, margin_required)
        self._safe_broker_entry(call, "SELL", qty, net_credit)
        return (f"📊 <b>PAPER OPEN SPREAD</b> {call.get('instrument')}\n"
                f"Short {short_strike}/{wing_strike}, net credit ₹{net_credit:,.2f}/unit | "
                f"total recv ₹{net_premium_received:,.2f}, margin ₹{margin_required:,.0f}")

    def _safe_broker_entry(self, call, action, qty, price) -> None:
        try:  # real-order hook (no-op in paper; double-guarded in live)
            self.broker.place_entry(call, action, qty, price)
        except Exception as e:  # noqa: BLE001
            log.error("Broker place_entry failed (%s): %s", call.get("instrument"), e)

    @staticmethod
    def _open_note(action, call, qty, lots, price, margin: float = 0.0) -> str:
        t1, t2, sl = _f(call.get("target_1")), _f(call.get("target_2")), _f(call.get("stop_loss"))
        targets = " / ".join(f"₹{t:,.2f}" for t in (t1, t2) if t is not None)
        note = (f"📝 <b>PAPER OPEN</b> {action} <b>{call.get('instrument')}</b>\n"
                f"×{qty} ({lots} lot{'s' if lots > 1 else ''}) @ ₹{price:,.2f}")
        if margin:
            note += f"  <i>(margin ₹{margin:,.0f})</i>"
        if targets:
            note += f"\n🎯 Target: {targets}"
        if sl is not None:
            note += f"\n🛑 Stop-loss: ₹{sl:,.2f}"
        return note

    # ── exits ─────────────────────────────────────────────────────────────────

    def _t1_exit_fraction(self, category: str) -> float:
        """Fraction of the position to book at T1 (rest rides to T2).

        Per-book via partial_fraction (default PAPER_PARTIAL_FRACTION = 0.6).
        """
        return self.partial_fraction

    @staticmethod
    def _margin_release(pos: dict, exit_qty: int) -> float:
        """Margin to free when closing `exit_qty` of a position (proportional)."""
        mb = float(pos.get("margin_blocked") or 0)
        total = int(pos.get("quantity") or 0)
        if mb <= 0 or total <= 0:
            return 0.0
        return round(mb * exit_qty / total, 2)

    def _book_partial(self, call: dict, exit_price: float | None, market: bool = False) -> str | None:
        """Book the T1 partial. The SL is already trailed to just below T1 by the
        tracker on the target1_hit event, so the profit is locked here either
        way — this only handles selling the partial quantity.

        `market=True` (time-stall partial) crosses the spread and is slipped against
        us; `market=False` (a T1 limit) fills at its level."""
        pos = get_open_paper_position_by_call(call.get("id"))
        if not pos or exit_price is None:
            return None
        lot_size = int(pos["lot_size"])
        action = str(pos["action"]).upper()
        entry = float(pos["entry_price"])
        if market:
            exit_price = _slip_fill(exit_price, "sell" if action == "BUY" else "buy", self.slippage_pct)
        locked_sl = t1_locked_stop(action, entry, exit_price)
        exit_fraction = self._t1_exit_fraction(pos.get("category", ""))
        partial_lots = math.floor(int(pos["lots"]) * exit_fraction)
        exit_qty = partial_lots * lot_size
        remaining = int(pos["remaining_qty"])

        # A lot is indivisible: a single-lot position (or a fraction that rounds to
        # zero lots) can't be split. Hold the whole position for T2 — the SL is
        # already locked below T1, so it exits in profit, never at a loss.
        if exit_qty < lot_size or exit_qty >= remaining:
            log.info("PAPER T1 %s: indivisible (%s lot) — holding for T2, SL locked at ₹%.2f",
                     pos["instrument"], pos["lots"], locked_sl)
            return (f"🎯 <b>PAPER T1 hit</b> <b>{pos['instrument']}</b> — holding "
                    f"{remaining} (can't split 1 lot) for T2\n"
                    f"🛑 SL trailed to ₹{locked_sl:,.2f} (just below T1) — profit locked")

        realized, cash_delta = _pnl_cash(action, entry, exit_price, exit_qty)
        book_paper_exit(
            position_id=pos["id"], call_id=pos["call_id"], instrument=pos["instrument"],
            exit_qty=exit_qty, exit_price=round(exit_price, 2),
            realized_delta=realized, cash_delta=cash_delta, kind="partial", fully_closed=False,
            strategy=self.strategy, margin_release=self._margin_release(pos, exit_qty),
        )
        log.info("PAPER PARTIAL %s ×%s (%.0f%%) @ %.2f (pnl %.0f) | SL locked at ₹%.2f",
                 pos["instrument"], exit_qty, exit_fraction * 100, exit_price, realized, locked_sl)
        try:  # real-order hook (no-op in paper; double-guarded in live)
            self.broker.place_exit(call, action, exit_qty, exit_price, "partial")
        except Exception as e:  # noqa: BLE001
            log.error("Broker place_exit (partial) failed (%s): %s", pos["instrument"], e)
        self._log_trade(pos, exit_qty, exit_price, realized, "partial")
        emoji = "🟢" if realized >= 0 else "🔴"
        return (f"💰 <b>PAPER T1</b> booked {exit_qty} ({exit_fraction * 100:.0f}%) of "
                f"<b>{pos['instrument']}</b> @ ₹{exit_price:,.2f} {emoji} ₹{realized:,.0f}\n"
                f"🛑 SL trailed to ₹{locked_sl:,.2f} (just below T1); "
                f"holding {remaining - exit_qty} for T2")

    def _close(self, call: dict, exit_price: float | None, kind: str, market: bool = True) -> str | None:
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
        # Market exits (stop/EOD/expiry/invalidation/manual) cross the spread and are
        # slipped against us; limit exits (targets) fill at their own level. For a
        # short the closing side is BUY (buy-to-close), so it slips UP — modelling
        # the tail honestly (a buy-back into a vol spike fills badly).
        if market:
            exit_price = _slip_fill(exit_price, "sell" if action == "BUY" else "buy", self.slippage_pct)
        gross, cash_delta = _pnl_cash(action, entry, exit_price, remaining)
        # Flat all-in round-trip cost is charged ONCE, here, on the final exit leg
        # (partials booked earlier paid nothing). Net = gross − cost; the cash also
        # leaves the account, so equity reflects the true take-home.
        cost = round(self.cost_per_trade, 2)
        realized = round(gross - cost, 2)
        book_paper_exit(
            position_id=pos["id"], call_id=pos["call_id"], instrument=pos["instrument"],
            exit_qty=remaining, exit_price=round(exit_price, 2),
            realized_delta=realized, cash_delta=round(cash_delta - cost, 2),
            kind=kind, fully_closed=True,
            strategy=self.strategy, margin_release=self._margin_release(pos, remaining),
        )
        log.info("PAPER CLOSE (%s) %s ×%s @ %.2f (gross %.0f − cost %.0f = net %.0f)",
                 kind, pos["instrument"], remaining, exit_price, gross, cost, realized)
        try:  # real-order hook (no-op in paper; double-guarded in live)
            self.broker.place_exit(
                {"category": pos.get("category"), "underlying": pos.get("underlying"),
                 "instrument": pos.get("instrument")},
                action, remaining, exit_price, kind)
        except Exception as e:  # noqa: BLE001
            log.error("Broker place_exit (%s) failed (%s): %s", kind, pos["instrument"], e)
        self._log_trade(pos, remaining, exit_price, realized, kind, cost=cost)
        label = {"exit": "🎯 PAPER TARGET", "stop": "🛑 PAPER STOP", "expiry": "⌛ PAPER EXPIRY",
                 "invalidated": "🔄 PAPER CUT (trend reversed)",
                 "eod": "🌙 PAPER EOD square-off",
                 "manual": "🙋 PAPER MANUAL CLOSE"}.get(kind, "PAPER CLOSE")
        emoji = "✅" if realized >= 0 else "❌"
        return (f"{label} <b>{pos['instrument']}</b> ×{remaining} @ ₹{exit_price:,.2f} "
                f"{emoji} ₹{realized:,.0f} <i>(after ₹{cost:,.0f} cost)</i>")

    @staticmethod
    def _log_trade(pos: dict, qty, exit_price, realized, kind: str, cost: float = 0.0) -> None:
        """Append a closed-trade record to the durable daily history.

        `realized` is NET of `cost`; gross_pnl is stored alongside for transparency.
        Partial legs pass cost=0 (the round-trip fee is charged on the final exit).
        """
        now = datetime.now()
        append_history({
            "date": now.strftime("%Y-%m-%d"),
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "position_id": int(pos["id"]) if pos.get("id") is not None else None,
            "call_id": pos.get("call_id"),
            "instrument": pos.get("instrument"),
            "underlying": pos.get("underlying"),
            "category": pos.get("category"),
            "action": str(pos.get("action")).upper(),
            "qty": int(qty),
            "entry": float(pos.get("entry_price")),
            "exit": round(float(exit_price), 2),
            "gross_pnl": round(float(realized) + float(cost), 2),
            "cost": round(float(cost), 2),
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
        open_pos = get_open_paper_positions(self.strategy)
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
                    "instrument": pos.get("instrument"), "option_expiry": exp}
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

    # ── manual close ─────────────────────────────────────────────────────────────

    def manual_close(self, call_id: int, price: float | None) -> str | None:
        """Close the open paper position for a call at `price` (Telegram /exit).

        No-op (returns None) if the call has no open paper position.
        """
        return self._close({"id": call_id}, _f(price), "manual")

    # ── EOD square-off ───────────────────────────────────────────────────────────

    def square_off_all(self, price_lookup) -> list[str]:
        """Close EVERY open position at the day's last price — nothing carries over.

        Used at EOD when EOD_SQUARE_OFF_ALL is on. Mirrors settle_expiry but with
        no expiry filter: every contract is exited at the live premium (falling
        back to last_price/entry when the feed is already down after close).
        """
        from data.advisory_store import get_call_levels

        notes: list[str] = []
        open_pos = get_open_paper_positions(self.strategy)
        if not open_pos:
            return notes
        levels = get_call_levels([p.get("call_id") for p in open_pos])
        for pos in open_pos:
            cid = pos.get("call_id")
            mini = {"category": pos.get("category"), "underlying": pos.get("underlying"),
                    "instrument": pos.get("instrument"),
                    "option_expiry": (levels.get(cid) or {}).get("option_expiry")}
            price = None
            try:
                price = price_lookup(mini)
            except Exception:  # noqa: BLE001
                price = None
            if price is None:
                price = float(pos["last_price"]) if pos["last_price"] is not None else float(pos["entry_price"])
            note = self._close({"id": cid}, float(price), "eod")
            if note:
                log.info("PAPER EOD SQUARE-OFF %s @ %.2f", pos.get("instrument"), float(price))
                notes.append(note)
        return notes

    # ── mark-to-market ─────────────────────────────────────────────────────────

    def mark_to_market(self, price_lookup) -> None:
        """Refresh open-position prices and record equity / drawdown."""
        from data.advisory_store import get_call_levels

        open_pos = get_open_paper_positions(self.strategy)
        levels = get_call_levels([p.get("call_id") for p in open_pos]) if open_pos else {}
        for pos in open_pos:
            mini_call = {
                "category": pos.get("category"),
                "underlying": pos.get("underlying"),
                "instrument": pos.get("instrument"),
                # Pin the quote to the position's actual contract — without this
                # the lookup falls back to today's nearest expiry.
                "option_expiry": (levels.get(pos.get("call_id")) or {}).get("option_expiry"),
            }
            try:
                price = price_lookup(mini_call)
            except Exception as e:  # noqa: BLE001
                log.debug("Paper MTM price lookup failed (%s): %s", pos.get("instrument"), e)
                price = None
            if price is not None:
                set_paper_position_last_price(pos["id"], round(float(price), 2))
        record_paper_equity(compute_paper_equity(self.strategy), self.strategy)
