"""
opening_scalp.py — deterministic opening gap-fade scalp (NO LLM).

One quick trade at the open, fading the gap (validated on 1-min Dhan data):
  • big gap-UP   → the minute-1 follow-through fades → buy ATM PE (enter ~min 2)
  • big gap-DOWN → the dip bounces                  → buy ATM CE (enter ~min 1)
Held a few minutes, then closed by a hard timer (or an optional premium target/
stop) BEFORE the market reconsolidates. Exactly one trade per day; afterwards the
engine stays quiet at the open (see GEN_RESUME_TIME) until normal generation.

This bypasses the slow LLM/tape path because the edge lives in the first 1-5
minutes. It is paper-safe (uses PaperTrader; cost-aware) and OFF by default —
enable with OPENING_SCALP_ENABLED to measure it live. Every step is wrapped so a
failure can never crash the main loop.
"""
from datetime import datetime, timedelta, time as dtime

from config.logger import get_logger
from config.settings import (
    OPENING_SCALP_ENABLED, SCALP_UNDERLYING, SCALP_GAP_MIN_PCT,
    SCALP_GAPUP_ENTRY_MIN, SCALP_GAPDN_ENTRY_MIN, SCALP_HOLD_MIN,
    SCALP_TARGET_PCT, SCALP_STOP_PCT, MARKET_OPEN,
)
from core.market_data_provider import (
    get_index_spots, get_option_chain, get_historical_daily,
)
from data.advisory_store import save_call, update_call_status

log = get_logger("opening_scalp")


class OpeningScalp:
    """Drives one gap-fade scalp at the open. Call step() every poll."""

    def __init__(self, session, paper):
        self.session = session
        self.paper = paper
        hh, mm = MARKET_OPEN.split(":")
        self._open_t = dtime(int(hh), int(mm))
        self._day = None
        self._reset(None)

    def _reset(self, day):
        self._day = day
        self._open_px = None
        self._prev_close = None
        self._plan = None       # (opt_type, entry_dt) | "skip" | None
        self._pos = None        # {call_id, instrument, underlying, expiry, entry_px, deadline}
        self._done = False

    # ── helpers ──────────────────────────────────────────────────────────────
    def _spot(self):
        try:
            spots = get_index_spots(self.session) or {}
        except Exception as e:  # noqa: BLE001
            log.debug("scalp spot fetch failed: %s", e)
            return None
        return spots.get(SCALP_UNDERLYING.lower()) or spots.get(SCALP_UNDERLYING.upper())

    def _prev_daily_close(self):
        try:
            bars = get_historical_daily(self.session, SCALP_UNDERLYING, days=12)
        except Exception as e:  # noqa: BLE001
            log.debug("scalp prev-close fetch failed: %s", e)
            return None
        return bars[-1]["close"] if bars else None

    # ── main step ────────────────────────────────────────────────────────────
    def step(self, now: datetime, price_lookup) -> list[str]:
        if not OPENING_SCALP_ENABLED or self.session is None or self.paper is None:
            return []
        if now.weekday() >= 5:
            return []
        today = now.date()
        if self._day != today:
            self._reset(today)
        if self._done or now.time() < self._open_t:
            return []

        # 1) Capture open + prev close on the first poll after the open; decide.
        if self._open_px is None:
            self._open_px = self._spot()
            self._prev_close = self._prev_daily_close()
            if not self._open_px or not self._prev_close:
                self._open_px = None  # retry next poll
                return []
            gap = (self._open_px - self._prev_close) / self._prev_close * 100
            open_dt = datetime.combine(today, self._open_t)
            if gap >= SCALP_GAP_MIN_PCT:
                self._plan = ("PE", open_dt + timedelta(minutes=SCALP_GAPUP_ENTRY_MIN))
                log.info("Opening scalp: gap +%.2f%% -> PE at %s", gap, self._plan[1].time())
            elif gap <= -SCALP_GAP_MIN_PCT:
                self._plan = ("CE", open_dt + timedelta(minutes=SCALP_GAPDN_ENTRY_MIN))
                log.info("Opening scalp: gap %.2f%% -> CE at %s", gap, self._plan[1].time())
            else:
                self._plan = "skip"
                self._done = True
                log.info("Opening scalp: gap %.2f%% < %.2f%% — no trade today", gap, SCALP_GAP_MIN_PCT)
            return []

        # 2) Entry once the planned minute arrives.
        if self._pos is None and self._plan and self._plan != "skip":
            opt_type, entry_dt = self._plan
            if now >= entry_dt:
                note = self._enter(opt_type, now)
                return [note] if note else []
            return []

        # 3) Manage the open scalp (timer / target / stop).
        if self._pos is not None:
            note = self._manage(now, price_lookup)
            return [note] if note else []
        return []

    # ── entry / exit ───────────────────────────────────────────────────────────
    def _enter(self, opt_type: str, now: datetime) -> str | None:
        self._done = True  # one attempt per day, win or lose
        try:
            chain = get_option_chain(self.session, SCALP_UNDERLYING) or {}
        except Exception as e:  # noqa: BLE001
            log.error("scalp option-chain fetch failed: %s", e)
            return None
        spot = chain.get("spot") or self._spot()
        legs = [r for r in chain.get("strikes", [])
                if str(r.get("option_type", "")).upper() == opt_type and r.get("premium")]
        if not legs or not spot:
            log.warning("scalp: no %s legs / spot for %s — skipping", opt_type, SCALP_UNDERLYING)
            return None
        atm = min(legs, key=lambda r: abs(r["strike"] - spot))
        premium = float(atm["premium"])
        strike = int(atm["strike"])
        if premium <= 0:
            return None
        instrument = f"{SCALP_UNDERLYING} {strike} {opt_type}"
        expiry = chain.get("expiry")
        # Far target/stop so the normal tracker stays inert during the few-minute
        # hold; the scalp's own timer/target/stop (in _manage) governs the exit.
        call = {
            "category": "index_option", "instrument": instrument, "underlying": SCALP_UNDERLYING,
            "action": "BUY", "timeframe": "intraday",
            "entry_price": round(premium, 2), "entry_min": round(premium, 2), "entry_max": round(premium, 2),
            "target_1": round(premium * 1.6, 2), "target_2": round(premium * 2.0, 2),
            "stop_loss": round(premium * 0.5, 2), "confidence": 100,
            "rationale": f"Opening gap-fade scalp ({opt_type}).", "option_expiry": expiry,
        }
        try:
            call_id = save_call(call)
            call["id"] = call_id
            update_call_status(call_id, "entry_triggered", last_price=premium, entry_triggered=True)
            note = self.paper._maybe_open(call, premium)
        except Exception as e:  # noqa: BLE001
            log.error("scalp entry failed (%s): %s", instrument, e)
            return None
        from data.advisory_store import get_open_paper_position_by_call
        if not get_open_paper_position_by_call(call_id):
            update_call_status(call_id, "closed", last_price=premium)  # didn't fill (e.g. unfunded)
            log.info("scalp: paper did not open %s (guardrail) — call closed", instrument)
            return note
        self._pos = {"call_id": call_id, "instrument": instrument, "underlying": SCALP_UNDERLYING,
                     "expiry": expiry, "entry_px": premium,
                     "deadline": now + timedelta(minutes=SCALP_HOLD_MIN)}
        log.info("Opening scalp ENTER %s @ %.2f (call #%s)", instrument, premium, call_id)
        return note or f"⚡ <b>Opening scalp</b> BUY {instrument} @ ₹{premium:,.2f}"

    def _manage(self, now: datetime, price_lookup) -> str | None:
        pos = self._pos
        mini = {"category": "index_option", "underlying": pos["underlying"],
                "instrument": pos["instrument"], "option_expiry": pos["expiry"]}
        px = None
        try:
            px = price_lookup(mini)
        except Exception:  # noqa: BLE001
            px = None
        if px is None:
            px = pos["entry_px"]
        px = float(px)
        entry = pos["entry_px"]
        chg = (px - entry) / entry * 100 if entry else 0.0

        reason = None
        if SCALP_TARGET_PCT > 0 and chg >= SCALP_TARGET_PCT:
            reason = "target"
        elif SCALP_STOP_PCT > 0 and chg <= -SCALP_STOP_PCT:
            reason = "stop"
        elif now >= pos["deadline"]:
            reason = "timer"
        if reason is None:
            update_call_status(pos["call_id"], "entry_triggered", last_price=px)  # keep LTP fresh
            return None

        try:
            note = self.paper.manual_close(pos["call_id"], px)
            update_call_status(pos["call_id"], "closed", last_price=px, result_pct=round(chg, 2))
        except Exception as e:  # noqa: BLE001
            log.error("scalp exit failed (%s): %s", pos["instrument"], e)
            note = None
        log.info("Opening scalp EXIT (%s) %s @ %.2f (%.2f%%)", reason, pos["instrument"], px, chg)
        self._pos = None
        return note or f"⚡ <b>Scalp exit</b> ({reason}) {pos['instrument']} @ ₹{px:,.2f} ({chg:+.2f}%)"
