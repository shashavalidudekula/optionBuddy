"""
main_multitenant.py — Multi-tenant F&O Trading Agent (Multiple users, multiple brokers)

Loop: every POLL_INTERVAL_SEC during market hours (9:15-15:30 IST)
  For each active user:
    1. Fetch open positions from user's broker
    2. Fetch market data
    3. Fetch news headlines (every 5 cycles)
    4. Ask Gemini for signal (personal portfolio mode)
    5. Save signal to PostgreSQL (with user_id)
    6. Send Telegram alert to user's chat_id (with approve/reject buttons)
    7. If signal is shared mode, broadcast to all users in shared mode

Scheduled tasks (same for all users):
  - 8:00-8:10 AM: Send morning briefing
  - 11:30-11:35 AM: Send mid-day briefing
  - 13:30-13:35 PM: Send afternoon news
  - 00:05-00:10 AM: Reset daily flags

Run:  python main_multitenant.py
Stop: Ctrl+C or /pause via Telegram
"""

import asyncio
import signal as os_signal
import sys
from datetime import datetime
from typing import Dict

from core.indstocks_auth import get_session
from core.user_context import UserContext
from data.market_briefing import format_briefing, get_market_outlook, format_midday_briefing
from core.market_data import get_all_market_data
from data.news_fetcher import get_top_headlines
from data.store import (
    init_db, get_conn, save_signal, mark_signal_acted, save_pnl_snapshot,
    get_all_active_users, get_user
)
from signals.claude_engine import get_signal
from signals.shared_signals import get_shared_signal
from notifier.telegram_bot_multitenant import TelegramBotMultitenant
from config.settings import POLL_INTERVAL_SEC, MARKET_OPEN, MARKET_CLOSE
from config.logger import get_logger

log = get_logger("main")

# ============================================================================
# Global State
# ============================================================================

users: Dict[str, UserContext] = {}  # user_id -> UserContext
bot: TelegramBotMultitenant = None


# ============================================================================
# Callbacks (for Telegram button presses)
# ============================================================================

async def on_approve(user_id: str, signal_id: int) -> None:
    """User approved a signal via Telegram button."""
    ctx = users.get(user_id)
    if not ctx:
        log.warning(f"Approval for unknown user {user_id}")
        return

    signal = ctx.pop_pending_signal(signal_id)
    if not signal:
        log.warning(f"Approval for unknown signal_id={signal_id}")
        return

    try:
        result = await ctx.executor.execute(signal, signal_id, approved=True)
        mark_signal_acted(signal_id, f"approved:{result['status']}", user_id)

        await bot.send_user_message(
            ctx.telegram_chat_id,
            f"✅ Order executed: {result['status']}"
        )
        log.info(f"Signal #{signal_id} approved and executed for {user_id}")
    except Exception as e:
        log.error(f"Error executing approved signal: {e}")
        await bot.send_user_message(
            ctx.telegram_chat_id,
            f"❌ Error executing order: {str(e)}"
        )


async def on_reject(user_id: str, signal_id: int) -> None:
    """User rejected a signal via Telegram button."""
    ctx = users.get(user_id)
    if not ctx:
        log.warning(f"Rejection for unknown user {user_id}")
        return

    ctx.pop_pending_signal(signal_id)
    mark_signal_acted(signal_id, "rejected_by_user", user_id)
    log.info(f"Signal #{signal_id} rejected by {user_id}")


# ============================================================================
# User Management
# ============================================================================

async def load_active_users(db) -> int:
    """Load all active (non-paused) users from database."""
    global users

    cursor = db.cursor()
    try:
        cursor.execute(
            "SELECT user_id, username, broker_type, signal_mode, is_paused, telegram_chat_id, broker_credentials "
            "FROM users WHERE is_paused = FALSE ORDER BY created_at"
        )

        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]

        for row in rows:
            user_data = dict(zip(columns, row))
            user_id = user_data.pop('user_id')
            encrypted_creds = user_data.pop('broker_credentials')

            try:
                ctx = UserContext.create(user_id, user_data, encrypted_creds)
                users[user_id] = ctx
            except Exception as e:
                log.error(f"Failed to load user {user_id}: {e}")

        log.info(f"Loaded {len(users)} active users")
        return len(users)

    finally:
        cursor.close()


async def refresh_user_list(db):
    """Refresh list of active users (run periodically to catch new registrations)."""
    global users

    try:
        # Get current active users from DB
        db_users = set()
        cursor = db.cursor()
        cursor.execute("SELECT user_id FROM users WHERE is_paused = FALSE")
        for row in cursor.fetchall():
            db_users.add(row[0])
        cursor.close()

        # Remove paused users from memory
        paused = set(users.keys()) - db_users
        for user_id in paused:
            log.info(f"Removing paused user: {user_id}")
            del users[user_id]

        # Load new users
        new_users = db_users - set(users.keys())
        if new_users:
            log.info(f"Loading {len(new_users)} new users")
            cursor = db.cursor()
            for user_id in new_users:
                try:
                    cursor.execute(
                        "SELECT user_id, username, broker_type, signal_mode, is_paused, telegram_chat_id, broker_credentials "
                        "FROM users WHERE user_id = %s",
                        (user_id,)
                    )
                    row = cursor.fetchone()
                    if row:
                        columns = [desc[0] for desc in cursor.description]
                        user_data = dict(zip(columns, row))
                        uid = user_data.pop('user_id')
                        encrypted_creds = user_data.pop('broker_credentials')
                        ctx = UserContext.create(uid, user_data, encrypted_creds)
                        users[uid] = ctx
                except Exception as e:
                    log.error(f"Failed to load new user {user_id}: {e}")
            cursor.close()

    except Exception as e:
        log.error(f"Error refreshing user list: {e}")


# ============================================================================
# Market Hours & Scheduling
# ============================================================================

def _is_market_hours() -> bool:
    """Check if current time is within market hours."""
    now = datetime.now().strftime("%H:%M")
    return MARKET_OPEN <= now <= MARKET_CLOSE


# ============================================================================
# Multi-User Trading Loop
# ============================================================================

async def run_trading_loop(db) -> None:
    """Main trading loop: runs for all users simultaneously."""
    log.info(f"Trading loop started. Polling every {POLL_INTERVAL_SEC}s")
    await bot.send_broadcast("🤖 *Trading agent started.* Monitoring F&O positions across all users.")

    loop_count = 0
    briefing_sent_today = False
    midday_briefing_sent_today = False
    afternoon_news_sent_today = False
    last_briefing_date = None
    last_shared_signal_time = None  # Track last market-wide shared signal

    while True:
        try:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            current_date = now.date()

            # ================================================================
            # SCHEDULED TASKS (same for all users)
            # ================================================================

            # 8 AM: Morning briefing
            if current_time >= "08:00" and current_time < "08:10":
                if not briefing_sent_today or last_briefing_date != current_date:
                    try:
                        log.info("Sending morning briefings to all users...")
                        outlook = await get_market_outlook(get_session())
                        briefing = format_briefing(get_session(), outlook)

                        for user_id, ctx in users.items():
                            try:
                                await bot.send_user_message(ctx.telegram_chat_id, briefing, parse_mode="HTML")
                            except Exception as e:
                                log.error(f"Failed to send briefing to {user_id}: {e}")

                        # Also send detailed news
                        await asyncio.sleep(2)
                        log.info("Sending market news headlines...")
                        try:
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

                                for user_id, ctx in users.items():
                                    try:
                                        await bot.send_user_message(ctx.telegram_chat_id, news_msg, parse_mode="HTML")
                                    except Exception as e:
                                        log.error(f"Failed to send news to {user_id}: {e}")
                        except Exception as e:
                            log.error(f"Error sending news: {e}")

                        briefing_sent_today = True
                        last_briefing_date = current_date
                    except Exception as e:
                        log.error(f"Error sending morning briefing: {e}")

            # 11:30 AM: Mid-day briefing
            if current_time >= "11:30" and current_time < "11:35":
                if not midday_briefing_sent_today:
                    try:
                        log.info("Sending mid-day briefings...")
                        midday_briefing = format_midday_briefing(get_session())

                        for user_id, ctx in users.items():
                            try:
                                await bot.send_user_message(ctx.telegram_chat_id, midday_briefing, parse_mode="HTML")
                            except Exception as e:
                                log.error(f"Failed to send midday briefing to {user_id}: {e}")

                        midday_briefing_sent_today = True
                    except Exception as e:
                        log.error(f"Error sending mid-day briefing: {e}")

            # 1:30 PM: Afternoon news
            if current_time >= "13:30" and current_time < "13:35":
                if not afternoon_news_sent_today:
                    try:
                        log.info("Sending afternoon news updates...")
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

                            for user_id, ctx in users.items():
                                try:
                                    await bot.send_user_message(ctx.telegram_chat_id, news_msg, parse_mode="HTML")
                                except Exception as e:
                                    log.error(f"Failed to send afternoon news to {user_id}: {e}")

                            afternoon_news_sent_today = True
                    except Exception as e:
                        log.error(f"Error sending afternoon news: {e}")

            # 10:00 AM & 2:00 PM: Generate and broadcast market-wide shared signals
            if current_time >= "09:55" and current_time < "10:05":
                if not last_shared_signal_time or (now - last_shared_signal_time).total_seconds() > 7200:  # 2 hours
                    try:
                        log.info("Generating market-wide shared signal...")
                        market = get_all_market_data(get_session())
                        headlines = get_top_headlines(limit=8)

                        shared_signal = await get_shared_signal(market, headlines)
                        if shared_signal and shared_signal.get("setup") != "HOLD":
                            log.info(f"Broadcasting shared signal: {shared_signal.get('setup')}")
                            await broadcast_market_wide_signal(shared_signal, db)
                            last_shared_signal_time = now
                    except Exception as e:
                        log.error(f"Error generating shared signal: {e}")

            elif current_time >= "13:55" and current_time < "14:05":
                if not last_shared_signal_time or (now - last_shared_signal_time).total_seconds() > 7200:  # 2 hours
                    try:
                        log.info("Generating afternoon market-wide shared signal...")
                        market = get_all_market_data(get_session())
                        headlines = get_top_headlines(limit=8)

                        shared_signal = await get_shared_signal(market, headlines)
                        if shared_signal and shared_signal.get("setup") != "HOLD":
                            log.info(f"Broadcasting shared signal: {shared_signal.get('setup')}")
                            await broadcast_market_wide_signal(shared_signal, db)
                            last_shared_signal_time = now
                    except Exception as e:
                        log.error(f"Error generating afternoon shared signal: {e}")

            # Midnight: Reset daily flags
            if current_time >= "00:05" and current_time < "00:10":
                if briefing_sent_today or midday_briefing_sent_today or afternoon_news_sent_today:
                    log.info("Resetting daily flags for new day")
                    briefing_sent_today = False
                    midday_briefing_sent_today = False
                    afternoon_news_sent_today = False
                    for ctx in users.values():
                        ctx.reset_daily_state()

            # ================================================================
            # PERIODIC: Refresh user list (every 60 cycles = ~20 mins)
            # ================================================================

            if loop_count % 60 == 0:
                await refresh_user_list(db)

            # ================================================================
            # PER-USER TRADING LOGIC
            # ================================================================

            if not _is_market_hours():
                if loop_count % 10 == 0:
                    log.info(f"Outside market hours ({datetime.now().strftime('%H:%M')}). Sleeping.")
                await asyncio.sleep(POLL_INTERVAL_SEC)
                loop_count += 1
                continue

            # Fetch news every 5 cycles
            headlines = []
            if loop_count % 5 == 0:
                headlines = get_top_headlines(limit=8)

            # Process each user
            for user_id, ctx in users.items():
                if ctx.is_paused:
                    continue

                try:
                    # Fetch positions
                    positions = await ctx.fetch_positions()
                    if not positions:
                        continue

                    # Fetch market data
                    market = get_all_market_data(get_session())

                    # Generate signal
                    pos_dicts = [p.to_dict() for p in positions]
                    signal = get_signal(pos_dicts, market, headlines)

                    if signal and signal.get("signal") != "HOLD":
                        # Attach security_id from matching position
                        sym = signal.get("instrument", "")
                        for p in positions:
                            if p.tradingsymbol == sym:
                                signal["security_id"] = p.security_id
                                break

                        # Save signal with user_id
                        signal_id = save_signal(
                            signal,
                            user_id=user_id,
                            signal_scope=ctx.signal_mode if ctx.signal_mode != "both" else "personal"
                        )

                        # Execute based on signal_mode
                        if ctx.signal_mode in ["shared", "both"]:
                            # Broadcast to all users in shared mode
                            await broadcast_shared_signal(signal, signal_id, user_id, db)

                        # Send alert to this user
                        ctx.add_pending_signal(signal_id, signal)
                        await bot.send_signal_alert(signal, signal_id, ctx.telegram_chat_id, user_id=user_id)

                    # Save P&L snapshot every 10 cycles
                    if loop_count % 10 == 0:
                        summary = await ctx.fetch_portfolio_summary()
                        save_pnl_snapshot(
                            summary.get("total_unrealised_pnl", 0),
                            summary.get("total_realised_pnl", 0),
                            summary.get("positions", []),
                            user_id=user_id
                        )

                except Exception as e:
                    log.error(f"Error trading for user {user_id}: {e}")
                    try:
                        await bot.send_user_message(
                            ctx.telegram_chat_id,
                            f"❌ Agent error: `{e}`"
                        )
                    except:
                        pass

        except Exception as e:
            log.error(f"Main loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL_SEC)
        loop_count += 1


# ============================================================================
# Shared Signal Broadcasting (Phase 5)
# ============================================================================

async def broadcast_market_wide_signal(shared_signal: dict, db) -> None:
    """Broadcast market-wide signal to all users in 'shared' or 'both' mode.

    This is for automatically-generated market signals (not from a user's portfolio).
    """
    try:
        # Save signal to DB first to get signal_id
        signal_id = save_signal(
            {**shared_signal, 'scope': 'shared'},
            user_id="system",  # System-generated signal
            signal_scope='shared'
        )

        cursor = db.cursor()
        cursor.execute(
            "SELECT user_id, telegram_chat_id FROM users "
            "WHERE signal_mode IN ('shared', 'both') AND is_paused = FALSE"
        )

        users_to_notify = cursor.fetchall()
        cursor.close()

        if not users_to_notify:
            log.info("No users subscribed to shared signals")
            return

        for user_id, chat_id in users_to_notify:
            try:
                # Send market-wide signal to user
                await bot.send_signal_alert(shared_signal, signal_id, chat_id, is_shared=True)

                # Add to pending signals
                ctx = users.get(user_id)
                if ctx:
                    ctx.add_pending_signal(signal_id, shared_signal)

                log.info(f"Sent market signal #{signal_id} to user {user_id}")

            except Exception as e:
                log.error(f"Failed to send market signal to user {user_id}: {e}")

    except Exception as e:
        log.error(f"Market-wide broadcast error: {e}")


async def broadcast_shared_signal(signal: dict, signal_id: int, sender_user_id: str, db) -> None:
    """Broadcast shared signal to all users in 'shared' or 'both' mode.

    This is when a user's personal signal is promoted to shared (if they have 'both' mode).
    """
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT user_id, telegram_chat_id FROM users "
            "WHERE signal_mode IN ('shared', 'both') AND is_paused = FALSE"
        )

        users_to_notify = cursor.fetchall()
        cursor.close()

        for user_id, chat_id in users_to_notify:
            try:
                # Save as shared signal for this user
                save_signal(
                    {**signal, 'shared_from': sender_user_id},
                    user_id=user_id,
                    signal_scope='shared'
                )

                # Add to pending signals
                ctx = users.get(user_id)
                if ctx:
                    ctx.add_pending_signal(signal_id, signal)
                    await bot.send_shared_signal_alert(signal, signal_id, chat_id, sender_user_id)

            except Exception as e:
                log.error(f"Failed to broadcast to user {user_id}: {e}")

    except Exception as e:
        log.error(f"Broadcast error: {e}")


# ============================================================================
# Shutdown
# ============================================================================

async def shutdown(loop_task: asyncio.Task) -> None:
    """Graceful shutdown."""
    log.info("Shutting down...")
    await bot.send_broadcast("🛑 *Trading agent stopped.*")
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass
    await bot.stop()


# ============================================================================
# Main
# ============================================================================

async def main() -> None:
    """Initialize and start the multi-tenant trading agent."""
    global bot

    # Initialize database
    init_db()

    # Load active users
    db = get_conn()
    await load_active_users(db)
    db.close()

    if not users:
        log.warning("No active users loaded. Agent running but idle.")

    # Initialize Telegram bot (multi-tenant)
    bot = TelegramBotMultitenant()
    bot.register_user_callbacks(on_approve, on_reject)
    await bot.start_polling()

    # Start trading loop
    loop_task = asyncio.create_task(run_trading_loop(get_conn()))

    # Handle signals (Unix only)
    loop = asyncio.get_event_loop()
    if sys.platform != "win32":
        loop.add_signal_handler(
            os_signal.SIGINT,
            lambda: asyncio.ensure_future(shutdown(loop_task))
        )
        loop.add_signal_handler(
            os_signal.SIGTERM,
            lambda: asyncio.ensure_future(shutdown(loop_task))
        )

    try:
        await loop_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
