"""
main.py -- Autonomous F&O Trading Agent (INDstocks + Gemini + Telegram)

Loop: every POLL_INTERVAL_SEC during market hours (9:15-15:30 IST)
  1. Fetch open F&O positions from INDstocks (indices, stocks, commodities, any instrument in portfolio)
  2. Fetch market data for tracked instruments (configurable via TRACKED_INDICES, TRACKED_STOCKS, TRACKED_COMMODITIES)
  3. Fetch news headlines (every 5 cycles)
  4. Ask Gemini for a signal
  5. Save signal to PostgreSQL
  6. Send Telegram alert with approve/reject buttons

Run:  python main.py
Stop: Ctrl+C  or  /pause via Telegram
"""
import asyncio
import signal as os_signal
import sys
from datetime import datetime

from core.indstocks_auth import get_session
from core.position_tracker import PositionTracker
from data.market_briefing import format_briefing, get_market_outlook, format_midday_briefing
from core.market_data import get_all_market_data
from data.news_fetcher import get_top_headlines
from data.store import init_db, save_signal, mark_signal_acted, save_pnl_snapshot
from signals.claude_engine import get_signal
from execution.order_executor import OrderExecutor
from notifier.telegram_bot import TelegramBot
from config.settings import POLL_INTERVAL_SEC, MARKET_OPEN, MARKET_CLOSE
from config.logger import get_logger

log = get_logger("main")

pending_signals: dict = {}
executor: OrderExecutor = None
bot: TelegramBot = None


async def on_approve(signal_id: int) -> None:
    signal = pending_signals.pop(signal_id, None)
    if not signal:
        log.warning("Approval for unknown signal_id=%d", signal_id)
        return
    result = executor.execute(signal, signal_id, approved=True)
    mark_signal_acted(signal_id, "approved:" + result["status"])
    await bot.send_message("Order result: `%s`" % result)


async def on_reject(signal_id: int) -> None:
    pending_signals.pop(signal_id, None)
    mark_signal_acted(signal_id, "rejected_by_user")


def _is_market_hours() -> bool:
    now = datetime.now().strftime("%H:%M")
    return MARKET_OPEN <= now <= MARKET_CLOSE


async def run_loop(session, tracker: PositionTracker) -> None:
    log.info("Agent loop started. Polling every %ds.", POLL_INTERVAL_SEC)
    await bot.send_message("*Trading agent started.* Monitoring F&O positions.")

    loop_count = 0
    briefing_sent_today = False
    midday_briefing_sent_today = False
    afternoon_news_sent_today = False
    last_briefing_date = None

    while True:
        try:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            current_date = now.date()

            # Send morning briefing at 8 AM (only once per day)
            if current_time >= "08:00" and current_time < "08:10":
                if not briefing_sent_today or last_briefing_date != current_date:
                    try:
                        log.info(f"[8 AM CHECK] Current time: {current_time}, Today: {briefing_sent_today}, Last date: {last_briefing_date}")
                        log.info("Sending morning market briefing...")
                        outlook = await get_market_outlook(session)
                        briefing = format_briefing(session, outlook)
                        await bot.send_morning_briefing(briefing)
                        log.info("Morning briefing sent successfully")

                        # Also send detailed news update after briefing
                        await asyncio.sleep(2)
                        log.info("Sending detailed market news...")
                        from data.news_fetcher import fetch_rss_headlines, fetch_newsapi_headlines
                        from datetime import datetime as dt

                        rss_news = fetch_rss_headlines()
                        newsapi_news = fetch_newsapi_headlines()
                        all_news = list(set(rss_news + newsapi_news))[:12]

                        if all_news:
                            news_msg = "<b>📰 MARKET NEWS AT 8 AM</b>\n"
                            news_msg += f"<i>{dt.now().strftime('%Y-%m-%d %H:%M IST')}</i>\n"
                            news_msg += "━" * 70 + "\n\n"
                            for i, headline in enumerate(all_news, 1):
                                news_msg += f"<b>{i}.</b> {headline}\n\n"
                            news_msg += "━" * 70
                            await bot.send_message(news_msg)
                            log.info(f"News update sent with {len(all_news)} headlines")
                        else:
                            log.warning("No news available to send")

                        briefing_sent_today = True
                        last_briefing_date = current_date
                    except Exception as e:
                        log.error("Error sending morning briefing: %s", e, exc_info=True)

            # Send mid-day briefing at 11:30 AM (only once per day)
            if current_time >= "11:30" and current_time < "11:35":
                if not midday_briefing_sent_today:
                    try:
                        log.info(f"[11:30 AM CHECK] Sending mid-day briefing...")
                        midday_briefing = format_midday_briefing(session)
                        await bot.send_midday_briefing(midday_briefing)
                        log.info("Mid-day briefing sent successfully")
                        midday_briefing_sent_today = True
                    except Exception as e:
                        log.error("Error sending mid-day briefing: %s", e, exc_info=True)

            # Send afternoon news update at 1:30 PM (only once per day)
            if current_time >= "13:30" and current_time < "13:35":
                if not afternoon_news_sent_today:
                    try:
                        log.info(f"[1:30 PM CHECK] Sending afternoon news update...")
                        from data.news_fetcher import fetch_rss_headlines, fetch_newsapi_headlines
                        from datetime import datetime as dt

                        rss_news = fetch_rss_headlines()
                        newsapi_news = fetch_newsapi_headlines()
                        all_news = list(set(rss_news + newsapi_news))[:15]

                        if all_news:
                            news_msg = "<b>📰 AFTERNOON NEWS UPDATE - 1:30 PM</b>\n"
                            news_msg += f"<i>{dt.now().strftime('%Y-%m-%d %H:%M IST')}</i>\n"
                            news_msg += "━" * 70 + "\n\n"
                            for i, headline in enumerate(all_news, 1):
                                news_msg += f"<b>{i}.</b> {headline}\n\n"
                            news_msg += "━" * 70
                            await bot.send_message(news_msg)
                            log.info(f"Afternoon news update sent with {len(all_news)} headlines")
                            afternoon_news_sent_today = True
                        else:
                            log.warning("No news available for afternoon update")
                            afternoon_news_sent_today = True
                    except Exception as e:
                        log.error("Error sending afternoon news update: %s", e, exc_info=True)

            # Reset daily flags at midnight (00:05 AM)
            if current_time >= "00:05" and current_time < "00:10":
                if briefing_sent_today or midday_briefing_sent_today or afternoon_news_sent_today:
                    log.info("Resetting daily briefing flags for new day")
                    briefing_sent_today = False
                    midday_briefing_sent_today = False
                    afternoon_news_sent_today = False
                    last_briefing_date = current_date

            if bot.is_paused:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            if not _is_market_hours():
                if loop_count % 10 == 0:
                    log.info("Outside market hours (%s). Sleeping.", datetime.now().strftime("%H:%M"))
                await asyncio.sleep(POLL_INTERVAL_SEC)
                loop_count += 1
                continue

            positions = tracker.fetch()
            if not positions:
                log.info("No open positions.")
                await asyncio.sleep(POLL_INTERVAL_SEC)
                loop_count += 1
                continue

            market = get_all_market_data(session)

            headlines = []
            if loop_count % 5 == 0:
                headlines = get_top_headlines(limit=8)

            pos_dicts = [p.to_dict() for p in positions]
            signal = get_signal(pos_dicts, market, headlines)

            if signal and signal.get("signal") != "HOLD":
                # Attach security_id from matching position so executor can place order
                sym = signal.get("instrument", "")
                for p in positions:
                    if p.tradingsymbol == sym:
                        signal["security_id"] = p.security_id
                        break

                signal_id = save_signal(signal)
                result = executor.execute(signal, signal_id, approved=False)

                if result["status"] == "pending_approval":
                    pending_signals[signal_id] = signal
                    await bot.send_signal_alert(signal, signal_id)
                elif result["status"] == "executed":
                    await bot.send_message("Auto-executed signal #%d: `%s`" % (signal_id, result))
                    mark_signal_acted(signal_id, "auto:" + result["status"])
                else:
                    await bot.send_message("Signal #%d rejected: %s" % (signal_id, result.get("reason")))

            if loop_count % 10 == 0:
                summary = tracker.summary()
                save_pnl_snapshot(summary["total_unrealised_pnl"], 0.0, summary["positions"])

        except Exception as e:
            log.error("Loop error: %s", e, exc_info=True)
            await bot.send_message("Agent error: `%s`" % e)

        await asyncio.sleep(POLL_INTERVAL_SEC)
        loop_count += 1


async def shutdown(loop_task: asyncio.Task) -> None:
    log.info("Shutting down...")
    await bot.send_message("Trading agent stopped.")
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
    await bot.stop()


async def main() -> None:
    global executor, bot

    init_db()

    log.info("Connecting to INDstocks...")
    session = get_session()

    tracker  = PositionTracker(session)
    executor = OrderExecutor(session)
    bot      = TelegramBot()
    bot.register_callbacks(on_approve, on_reject)

    await bot.start_polling()
    loop_task = asyncio.create_task(run_loop(session, tracker))

    loop = asyncio.get_event_loop()
    if sys.platform != "win32":
        loop.add_signal_handler(os_signal.SIGINT,  lambda: asyncio.ensure_future(shutdown(loop_task)))
        loop.add_signal_handler(os_signal.SIGTERM, lambda: asyncio.ensure_future(shutdown(loop_task)))

    try:
        await loop_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
