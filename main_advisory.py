"""
main_advisory.py — Orchestrator for the OptionBuddy advisory product.

Advisory-only: it researches the market, publishes trade-idea "calls" (entry,
target(s), stop-loss + rationale) and tracks each call's lifecycle, pushing
Telegram alerts to subscribers. There is NO order execution and NO broker link.

Loop responsibilities:
  • Startup       — init advisory DB, start the Telegram bot polling.
  • Generation    — every GENERATION_INTERVAL_MIN during market hours, ask the
                    AI engine for fresh high-conviction calls per category,
                    persist them and push to category subscribers.
  • Tracking      — every poll, evaluate active calls against live prices and
                    push entry/target/SL/expiry events.
  • Briefing      — once each trading morning, broadcast a "good morning" note
                    with the 30-day track record.
  • EOD summary   — once after close, broadcast the day's track-record digest.

All heavy/blocking work (Gemini, yfinance, news, DB) runs in worker threads so
the Telegram event loop stays responsive.
"""

import asyncio
from datetime import datetime, time as dtime

from config.logger import get_logger
from config.settings import POLL_INTERVAL_SEC, MARKET_OPEN, MARKET_CLOSE
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
from core.advisory_market_data import get_market_snapshot, make_price_lookup
from data.news_fetcher import get_top_headlines
from notifier.telegram_advisory_bot import (
    TelegramAdvisoryBot,
    CATEGORY_LABEL,
    DISCLAIMER,
)

log = get_logger("advisory_main")

# How often to scan the market for fresh calls (minutes).
GENERATION_INTERVAL_MIN = 30
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

def _generate_all_categories() -> list[tuple[dict, int]]:
    """Generate + persist fresh calls across all categories. Returns (call, id)."""
    headlines = get_top_headlines()
    market = get_market_snapshot()
    exclude = active_instruments()

    saved: list[tuple[dict, int]] = []
    for cat in CATEGORIES:
        try:
            calls = generate_calls(cat, market, headlines, exclude_instruments=exclude)
        except Exception as e:
            log.error("Generation failed for %s: %s", cat, e)
            continue
        for call in calls:
            try:
                call_id = save_call(call)
            except Exception as e:
                log.error("save_call failed (%s): %s", call.get("instrument"), e)
                continue
            saved.append((call, call_id))
            exclude.add(call.get("instrument"))  # avoid dupes within this cycle
    return saved


# ── Async cycles ──────────────────────────────────────────────────────────────

async def run_generation_cycle(bot: TelegramAdvisoryBot) -> None:
    saved = await asyncio.to_thread(_generate_all_categories)
    for call, call_id in saved:
        await bot.push_new_call(call, call_id)
    log.info("Generation cycle published %s call(s)", len(saved))


async def run_tracking_pass(bot: TelegramAdvisoryBot, price_lookup) -> None:
    events = await asyncio.to_thread(track_active_calls, price_lookup)
    for evt in events:
        await bot.push_call_event(evt)
    if events:
        log.info("Tracking pass emitted %s event(s)", len(events))


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

    bot = TelegramAdvisoryBot()
    await bot.start_polling()

    price_lookup = make_price_lookup()

    last_gen: datetime | None = None
    briefing_date = None
    eod_date = None

    log.info("Advisory orchestrator started (poll=%ss, gen=%smin)",
             POLL_INTERVAL_SEC, GENERATION_INTERVAL_MIN)

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
                # Fresh-call generation on its own cadence.
                if (last_gen is None
                        or (now - last_gen).total_seconds() >= GENERATION_INTERVAL_MIN * 60):
                    try:
                        await run_generation_cycle(bot)
                    except Exception as e:
                        log.error("Generation cycle failed: %s", e)
                    last_gen = now

                # Lifecycle tracking every poll.
                try:
                    await run_tracking_pass(bot, price_lookup)
                except Exception as e:
                    log.error("Tracking pass failed: %s", e)

            # EOD digest — once per trading day, after close.
            if (_is_weekday(now) and now.time() >= EOD_AFTER and eod_date != today):
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
