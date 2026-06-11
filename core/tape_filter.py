"""
tape_filter.py — reactive "don't fight the tape" guard.

The LLM can't reliably PREDICT intraday direction, so bearish calls leak out during
a rally ("PUTs into a rally"). This module does NOT predict — it reads the REALIZED
intraday tape (day move, 30-min momentum, 5-min EMA trend, opening-range) and acts
on what has already happened:

  Layer 2 (entry):  confirm_call / filter_calls  — drop a new call whose direction
                    fights a clear trend (e.g. BUY PE while the index is clearly UP).
  Layer 3 (exit):   is_invalidated               — flag an open call whose underlying
                    has DECISIVELY reversed against it (stronger threshold).

Reuses the intraday signals already computed in core/technicals. Because it only
reacts to realized data, it can't be "wrong about the future". When the tape is
flat/choppy nothing is blocked or cut.
"""

import re

from config.logger import get_logger
from config.settings import (
    TAPE_FILTER_ENABLED,
    TAPE_MIN_MOVE_PCT,
    TAPE_EXIT_ENABLED,
    TAPE_EXIT_MOVE_PCT,
    TAPE_REVERSAL_PCT,
    TAPE_EXIT_REVERSAL_PCT,
)
from core.technicals import get_intraday_technicals

log = get_logger("tape_filter")

# Underlyings we have intraday context for (mirrors technicals._YF_TICKER).
_GATEABLE = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"}
_OPTION_RE = re.compile(r"\b(CE|PE)\b", re.IGNORECASE)


def _call_view(call: dict) -> str | None:
    """Directional view on the UNDERLYING: 'up' | 'down' | None.

    Options: BUY CE / SELL PE → up ; BUY PE / SELL CE → down.
    Futures: BUY → up ; SELL → down.
    """
    action = str(call.get("action", "")).upper()
    if action not in ("BUY", "SELL"):
        return None
    if call.get("category") == "index_option":
        m = _OPTION_RE.search(str(call.get("instrument", "")))
        if not m:
            return None
        opt = m.group(1).upper()
        bull = (action == "BUY" and opt == "CE") or (action == "SELL" and opt == "PE")
        return "up" if bull else "down"
    return "up" if action == "BUY" else "down"


def tape_direction(
    underlying: str,
    min_move: float = TAPE_MIN_MOVE_PCT,
    reversal_move: float = TAPE_REVERSAL_PCT,
) -> tuple[str, str]:
    """Realized intraday direction: ('up'|'down'|'flat', human reason).

    Two reads, reversal first:
      1. Reversal off the day's extremes — price pulled back >= reversal_move
         from the day high (or bounced off the low) with momentum agreeing.
         This catches the afternoon slide that starts from a big morning gain:
         the from-open change stays positive all the way down, so rule 2 alone
         reads it as "up" while the index is falling (the failure mode that kept
         buying CALLs into a 1 PM sell-off).
      2. From-open trend — a real day move (>= min_move) with 30-min momentum,
         5-min EMA trend and opening-range agreement, but only while price is
         still near the extreme in that direction (not after a reversal-sized
         pullback). Anything short of that is 'flat' (we never act on chop).
    """
    u = underlying.upper()
    tech = get_intraday_technicals([u]).get(u)
    if not tech:
        return "flat", "no-data"
    chg = tech.get("intraday_change_pct")
    mom = tech.get("momentum_30m_pct") or 0.0
    mom15 = tech.get("momentum_15m_pct") or 0.0
    trend = tech.get("trend_5m")
    ors = tech.get("opening_range_state")
    off_high = tech.get("pct_from_day_high") or 0.0   # <= 0
    off_low = tech.get("pct_from_day_low") or 0.0     # >= 0
    last_extreme = tech.get("last_extreme")           # which extreme printed last
    if chg is None:
        return "flat", "no-change-data"

    # 1) Reversal off the day's extremes (overrides the from-open read). Only the
    #    most recently printed extreme counts — on a green day price sits far above
    #    the morning low ALL day, which isn't an "up reversal"; it only becomes one
    #    if a fresh low printed and price is bouncing off it (and vice versa).
    if (off_high <= -reversal_move and min(mom, mom15) <= 0 and trend != "up"
            and last_extreme != "low"):
        return "down", (f"reversal {off_high:+.2f}% off day high, day {chg:+.2f}%, "
                        f"30m {mom:+.2f}%, 15m {mom15:+.2f}%, 5m {trend}")
    if (off_low >= reversal_move and max(mom, mom15) >= 0 and trend != "down"
            and last_extreme != "high"):
        return "up", (f"reversal {off_low:+.2f}% off day low, day {chg:+.2f}%, "
                      f"30m {mom:+.2f}%, 15m {mom15:+.2f}%, 5m {trend}")

    # 2) From-open trend — only while price is still near the day's extreme in
    #    that direction; a reversal-sized pullback demotes it to 'flat'.
    if (chg >= min_move and mom >= 0 and trend != "down" and ors != "below_OR_low"
            and off_high > -reversal_move):
        return "up", f"day {chg:+.2f}%, 30m {mom:+.2f}%, 5m {trend}"
    if (chg <= -min_move and mom <= 0 and trend != "up" and ors != "above_OR_high"
            and off_low < reversal_move):
        return "down", f"day {chg:+.2f}%, 30m {mom:+.2f}%, 5m {trend}"
    return "flat", f"day {chg:+.2f}%, {off_high:+.2f}% off high (no clear trend)"


# ── Layer 2: entry gate ──────────────────────────────────────────────────────

def confirm_call(call: dict) -> tuple[bool, str]:
    """(allow, reason). Blocks only a clear directional conflict with the tape."""
    if not TAPE_FILTER_ENABLED:
        return True, "tape-off"
    view = _call_view(call)
    if view is None:
        return True, "no-view"
    u = str(call.get("underlying", "")).upper()
    if u not in _GATEABLE:
        return True, "ungated-underlying"
    direction, why = tape_direction(u)
    if direction == "flat":
        return True, f"tape-flat ({why})"
    if direction == view:
        return True, f"tape-aligned-{view} ({why})"
    return False, f"tape-conflict: {view} call vs {direction} tape ({why})"


def filter_calls(calls: list[dict]) -> list[dict]:
    """Drop calls that fight a clear intraday trend. Logs each block."""
    if not TAPE_FILTER_ENABLED or not calls:
        return calls
    kept: list[dict] = []
    for call in calls:
        allow, reason = confirm_call(call)
        if allow:
            kept.append(call)
        else:
            log.info("Tape BLOCKED %s %s — %s",
                     call.get("action"), call.get("instrument"), reason)
    return kept


# ── Layer 3: exit / invalidation ─────────────────────────────────────────────

def is_invalidated(call: dict) -> tuple[bool, str]:
    """True when the underlying has DECISIVELY reversed against an open call.

    Uses the stronger TAPE_EXIT_MOVE_PCT so we cut losers on a real reversal, not
    on noise. Returns (should_cut, reason).
    """
    if not (TAPE_FILTER_ENABLED and TAPE_EXIT_ENABLED):
        return False, "exit-off"
    view = _call_view(call)
    if view is None:
        return False, "no-view"
    u = str(call.get("underlying", "")).upper()
    if u not in _GATEABLE:
        return False, "ungated-underlying"
    direction, why = tape_direction(u, min_move=TAPE_EXIT_MOVE_PCT,
                                    reversal_move=TAPE_EXIT_REVERSAL_PCT)
    if direction != "flat" and direction != view:
        return True, f"tape reversed to {direction} vs {view} call ({why})"
    return False, f"tape-ok ({direction})"
