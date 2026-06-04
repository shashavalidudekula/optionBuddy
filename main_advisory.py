"""
main_advisory.py — Orchestrator for the OptionBuddy advisory product.

Advisory-only: it researches the market, publishes trade-idea "calls" (entry,
target(s), stop-loss + rationale) and tracks each call's lifecycle, pushing
Telegram alerts to subscribers. There is NO order execution and NO broker link.

Loop responsibilities (fast loop every POLL_INTERVAL_SEC, ~5s):
  • Startup       — init advisory DB, start the Telegram bot polling.
  • Tracking      — every poll, evaluate active calls against live prices and
                    push entry/target/SL/expiry events (this is the near-real-time
                    layer: it fires the instant a premium hits its entry/exit level).
  • Option gen    — EVENT-DRIVEN: re-scan index options the moment the market moves
                    (index % move, new ATM strike, VIX jump) or at least every
                    OPT_GEN_FLOOR_SEC, with an OPT_GEN_MIN_GAP_SEC cooldown.
  • Other gen     — equity/futures/commodity on a slower OTHER_GEN_INTERVAL_MIN timer.
  • Briefing      — once each trading morning, broadcast a "good morning" note.
  • EOD summary   — once after close, broadcast the day's track-record digest.

All heavy/blocking work (LLM, yfinance, news, DB) runs in worker threads so the
Telegram event loop stays responsive.
"""

import asyncio
import time
from datetime import datetime, time as dtime

from config.logger import get_logger
from config.settings import (
    POLL_INTERVAL_SEC, MARKET_OPEN, MARKET_CLOSE, PAPER_TRADING_ENABLED,
    OPT_GEN_MIN_GAP_SEC, OPT_GEN_FLOOR_SEC, GEN_MOVE_PCT, GEN_VIX_JUMP_PCT,
    OTHER_GEN_INTERVAL_MIN, ATM_STEP,
)
from data.advisory_store import (
    CATEGORIES,
    init_advisory_db,
    save_call,
    active_instruments,
    get_track_record,
    get_active_calls,
)
from signals.advisory_engine import generate_calls
from core.call_tracker import track_active_calls
from core.paper_trader import PaperTrader
from core.indstocks_auth import get_session
from core.indstocks_data import (
    get_market_snapshot, make_price_lookup, get_option_chain, get_index_spots,
    option_expiry_for,
)
from data.news_fetcher import get_top_headlines
from notifier.telegram_advisory_bot import (
    TelegramAdvisoryBot,
    CATEGORY_LABEL,
    DISCLAIMER,
)

log = get_logger("advisory_main")

# Don't start broadcasting the morning briefing before this time.
BRIEFING_AFTER = dtime(8, 30)
# Run the EOD digest after the market closes.
EOD_AFTER = dtime(15, 35)


def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


_OPEN_T = _parse_hhmm(MARKET_OPEN)
_CLOSE_T = _parse_hhmm(MARKET_CLOSE)


def _is_weekday(now: datetime) -> bool:
    return now.weekday() < 5  # Mon–Fri


def _in_market_hours(now: datetime) -> bool:
    return _is_weekday(now) and _OPEN_T <= now.time() <= _CLOSE_T


# ── Work units (sync, run in threads) ─────────────────────────────────────────

def _generate_all_categories(session, categories) -> list[tuple[dict, int]]:
    """Generate + persist fresh calls for the given categories. Returns (call, id)."""
    headlines = get_top_headlines()
    market = get_market_snapshot(session)
    exclude = active_instruments()

    saved: list[tuple[dict, int]] = []
    for cat in categories:
        cat_market = market
        if cat == "index_option" and session is not None:
            # Ground option calls in real, tradeable premiums (nearest expiry, ATM±N).
            try:
                cat_market = {
                    **market,
                    "option_chain": {
                        "NIFTY": get_option_chain(session, "NIFTY"),
                        "BANKNIFTY": get_option_chain(session, "BANKNIFTY"),
                        "SENSEX": get_option_chain(session, "SENSEX"),
                    },
                }
            except Exception as e:
                log.warning("Option-chain fetch failed: %s", e)
        try:
            calls = generate_calls(cat, cat_market, headlines, exclude_instruments=exclude)
        except Exception as e:
            log.error("Generation failed for %s: %s", cat, e)
            continue
        for call in calls:
            # Stamp the option's real contract expiry (for display + EOD settlement).
            if cat == "index_option" and session is not None:
                try:
                    exp = option_expiry_for(session, call.get("underlying", ""), call.get("instrument", ""))
                    if exp:
                        call["option_expiry"] = exp.isoformat()
                except Exception as e:  # noqa: BLE001
                    log.debug("Expiry resolve failed (%s): %s", call.get("instrument"), e)
            try:
                call_id = save_call(call)
            except Exception as e:
                log.error("save_call failed (%s): %s", call.get("instrument"), e)
                continue
            saved.append((call, call_id))
            exclude.add(call.get("instrument"))  # avoid dupes within this cycle
    return saved


# ── Async cycles ──────────────────────────────────────────────────────────────

async def run_generation_cycle(bot: TelegramAdvisoryBot, session, categories) -> None:
    saved = await asyncio.to_thread(_generate_all_categories, session, categories)
    for call, call_id in saved:
        await bot.push_new_call(call, call_id)
    if saved:
        log.info("Generation (%s) published %s call(s)", ",".join(categories), len(saved))


class OptionGenTrigger:
    """Decides when to re-scan index options — on real market moves, not a timer.

    Fires when an index moves >= GEN_MOVE_PCT, crosses into a new ATM strike, or
    India VIX jumps >= GEN_VIX_JUMP_PCT since the last scan; plus a floor so we
    scan at least every OPT_GEN_FLOOR_SEC, and a cooldown so we never scan more
    often than OPT_GEN_MIN_GAP_SEC.

    Forces an immediate scan at market open (9:15 AM) to catch early breakouts.
    """

    def __init__(self):
        self.last_gen = 0.0
        self.ref_spot: dict[str, float] = {}
        self.ref_atm: dict[str, float] = {}
        self.ref_vix: float | None = None
        self.market_open_scanned = False

    @staticmethod
    def _atm(index: str, spot: float | None) -> float | None:
        step = ATM_STEP.get(index)
        if not step or not spot:
            return None
        return round(spot / step) * step

    def check(self, spots: dict) -> tuple[bool, str]:
        now = datetime.now()
        now_ts = time.time()

        # Force scan at market open (9:15 AM) to catch early breakouts
        if (not self.market_open_scanned and now.time() >= _OPEN_T
                and not self.ref_spot):
            return True, "market-open"

        if now_ts - self.last_gen < OPT_GEN_MIN_GAP_SEC:
            return False, ""
        if not self.ref_spot:
            return True, "init"

        reasons: list[str] = []
        if now_ts - self.last_gen >= OPT_GEN_FLOOR_SEC:
            reasons.append("floor")
        for lbl, idx in (("nifty", "NIFTY"), ("banknifty", "BANKNIFTY"), ("sensex", "SENSEX")):
            cur, ref = spots.get(lbl), self.ref_spot.get(lbl)
            if cur and ref and abs(cur - ref) / ref * 100 >= GEN_MOVE_PCT:
                reasons.append(f"{idx} {(cur - ref) / ref * 100:+.2f}%")
            atm, ratm = self._atm(idx, cur), self.ref_atm.get(lbl)
            if atm and ratm and atm != ratm:
                reasons.append(f"{idx} ATM→{int(atm)}")
        vix = spots.get("indiavix")
        if vix and self.ref_vix and abs(vix - self.ref_vix) / self.ref_vix * 100 >= GEN_VIX_JUMP_PCT:
            reasons.append(f"VIX {(vix - self.ref_vix) / self.ref_vix * 100:+.1f}%")
        return (bool(reasons), ", ".join(reasons))

    def commit(self, spots: dict) -> None:
        self.last_gen = time.time()
        self.market_open_scanned = True
        for lbl, idx in (("nifty", "NIFTY"), ("banknifty", "BANKNIFTY"), ("sensex", "SENSEX")):
            if spots.get(lbl):
                self.ref_spot[lbl] = spots[lbl]
                self.ref_atm[lbl] = self._atm(idx, spots[lbl])
        if spots.get("indiavix"):
            self.ref_vix = spots["indiavix"]


async def run_tracking_pass(bot: TelegramAdvisoryBot, price_lookup, paper: PaperTrader | None) -> None:
    events = await asyncio.to_thread(track_active_calls, price_lookup)
    for evt in events:
        await bot.push_call_event(evt)
    if events:
        log.info("Tracking pass emitted %s event(s)", len(events))

    # Shadow/paper trading reacts to the same lifecycle events (zero real money).
    if paper is not None:
        await asyncio.to_thread(paper.mark_to_market, price_lookup)
        notes = await asyncio.to_thread(paper.process_events, events)
        for note in notes:
            await bot.notify_owner(note)


def _track_record_lines(title: str) -> list[str]:
    rec = get_track_record(days=30)
    o = rec["overall"]
    lines = [
        title,
        "",
        f"Calls closed (30d): *{o['total']}*",
        f"Win rate: *{o['win_rate']}%*  (avg {o['avg_return']}%/call)",
    ]
    by_cat = [
        f"• {CATEGORY_LABEL.get(c, c)}: {s['win_rate']}% ({s['wins']}/{s['total']})"
        for c, s in rec["by_category"].items() if s["total"]
    ]
    if by_cat:
        lines += ["", *by_cat]
    return lines


async def send_morning_briefing(bot: TelegramAdvisoryBot) -> None:
    active = await asyncio.to_thread(get_active_calls)
    lines = [
        "☀️ *Good morning from OptionBuddy!*",
        "",
        f"{len(active)} call(s) still live from before. We'll scan Index Options, "
        "Equity, Futures & Commodity through the day and alert you on every fresh "
        "setup and every target/stop-loss.",
        "",
        *_track_record_lines("📊 *Our last 30 days*"),
        "",
        DISCLAIMER,
    ]
    await bot.broadcast("\n".join(lines))
    log.info("Morning briefing broadcast")


async def send_eod_summary(bot: TelegramAdvisoryBot) -> None:
    lines = [
        "🌙 *Market closed — daily wrap*",
        "",
        *_track_record_lines("Here's how our calls are tracking:"),
        "",
        "Rest up — fresh setups tomorrow. " + DISCLAIMER,
    ]
    await bot.broadcast("\n".join(lines))
    log.info("EOD summary broadcast")


# ── Main loop ──────────────────────────────────────────────────────────────────

async def run() -> None:
    init_advisory_db()
    log.info("Advisory DB ready")

    # INDstocks is the live market-data feed (data only — no execution).
    try:
        session = get_session()
    except Exception as e:
        session = None
        log.error("INDstocks session unavailable (%s). Running with global cues only; "
                  "option/equity tracking will rely on expiry until a token is set.", e)

    bot = TelegramAdvisoryBot()
    await bot.start_polling()

    price_lookup = make_price_lookup(session)

    paper: PaperTrader | None = None
    if PAPER_TRADING_ENABLED:
        try:
            paper = PaperTrader(session)
        except Exception as e:
            log.error("Paper trader init failed (%s); continuing without it.", e)

    opt_trigger = OptionGenTrigger()
    other_categories = [c for c in CATEGORIES if c != "index_option"]
    last_other_gen: datetime | None = None
    briefing_date = None
    eod_date = None

    log.info("Advisory orchestrator started (poll=%ss, options=event-driven, others=%smin)",
             POLL_INTERVAL_SEC, OTHER_GEN_INTERVAL_MIN)

    try:
        while True:
            now = datetime.now()
            today = now.date()

            # Morning briefing — once per trading day.
            if (_is_weekday(now) and now.time() >= BRIEFING_AFTER
                    and now.time() <= _CLOSE_T and briefing_date != today):
                try:
                    await send_morning_briefing(bot)
                except Exception as e:
                    log.error("Morning briefing failed: %s", e)
                briefing_date = today

            if _in_market_hours(now):
                # 1) Lifecycle tracking every poll — the near-real-time entry/exit layer.
                try:
                    await run_tracking_pass(bot, price_lookup, paper)
                except Exception as e:
                    log.error("Tracking pass failed: %s", e)

                # 2) Event-driven index-option generation — fire when the market moves.
                #    Skip when spots are unavailable (e.g. feed hiccup) so we don't
                #    fire blindly or hammer the quote API without ATM grounding.
                if session is not None:
                    try:
                        spots = await asyncio.to_thread(get_index_spots, session)
                        if spots:
                            fire, reason = opt_trigger.check(spots)
                            if fire:
                                log.info("Option scan triggered (%s)", reason or "—")
                                await run_generation_cycle(bot, session, ["index_option"])
                                opt_trigger.commit(spots)
                    except Exception as e:
                        log.error("Option generation failed: %s", e)

                # 3) Slower cadence for equity/futures/commodity.
                if other_categories and (
                        last_other_gen is None
                        or (now - last_other_gen).total_seconds() >= OTHER_GEN_INTERVAL_MIN * 60):
                    try:
                        await run_generation_cycle(bot, session, other_categories)
                    except Exception as e:
                        log.error("Other-category generation failed: %s", e)
                    last_other_gen = now

            # EOD digest — once per trading day, after close.
            if (_is_weekday(now) and now.time() >= EOD_AFTER and eod_date != today):
                # Settle paper positions whose options expire today (cash-settled at close).
                if paper is not None:
                    try:
                        notes = await asyncio.to_thread(paper.settle_expiry, price_lookup, today)
                        for note in notes:
                            await bot.notify_owner(note)
                        if notes:
                            log.info("Expiry settlement closed %s position(s)", len(notes))
                    except Exception as e:
                        log.error("Expiry settlement failed: %s", e)
                try:
                    await send_eod_summary(bot)
                except Exception as e:
                    log.error("EOD summary failed: %s", e)
                eod_date = today

            await asyncio.sleep(POLL_INTERVAL_SEC)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown requested")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(run())
