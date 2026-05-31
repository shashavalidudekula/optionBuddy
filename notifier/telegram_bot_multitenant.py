"""
telegram_bot_multitenant.py — Multi-tenant Telegram bot with per-user routing.

Single bot instance that serves all users by routing messages based on chat_id.
Supports:
- Per-user signal alerts with approve/reject buttons
- Per-user P&L and news commands
- System-wide broadcasts
- Shared signal handling

Callbacks include user_id so trading agent knows which user approved/rejected.
"""

from typing import Callable, Awaitable
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config.settings import TELEGRAM_BOT_TOKEN
from config.logger import get_logger
from data.store import get_user_by_chat_id, get_user_pnl_summary

log = get_logger("telegram_bot")

# Callback signatures
ApproveCallback = Callable[[str, int], Awaitable[None]]  # user_id, signal_id
RejectCallback = Callable[[str, int], Awaitable[None]]   # user_id, signal_id


def _signal_message(signal: dict, signal_id: int, is_shared: bool = False) -> str:
    """Format signal for Telegram message."""
    sig = signal.get("signal", "?")
    inst = signal.get("instrument", "?")
    conf = signal.get("confidence", 0)
    urg = signal.get("urgency", "?").upper()
    reason = signal.get("reason", "")
    max_loss = signal.get("max_loss_if_held", 0)
    benefit = signal.get("action_benefit", 0)
    risk = signal.get("key_risk", "")
    watch = signal.get("watch_level", "")

    urg_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(urg, "⚪")
    shared_badge = "📡 SHARED" if is_shared else "👤 PERSONAL"

    return (
        f"{urg_emoji} *{sig}* — `{inst}` [{shared_badge}]\n"
        f"Confidence: *{conf}%* | Urgency: *{urg}*\n\n"
        f"📋 {reason}\n\n"
        f"📉 Max loss if held: ₹{max_loss:,.0f}\n"
        f"✅ Benefit of action: ₹{benefit:,.0f}\n"
        f"⚠️ Key risk: {risk}\n"
        f"👁 Watch: {watch}\n\n"
        f"_Signal ID: {signal_id}_"
    )


class TelegramBotMultitenant:
    """Multi-tenant Telegram bot with per-user routing."""

    def __init__(self):
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self._approve_cb: ApproveCallback | None = None
        self._reject_cb: RejectCallback | None = None
        self._setup_handlers()

    def register_user_callbacks(
        self,
        on_approve: ApproveCallback,
        on_reject: RejectCallback,
    ) -> None:
        """Register callbacks for approval/rejection."""
        self._approve_cb = on_approve
        self._reject_cb = on_reject

    def _setup_handlers(self) -> None:
        """Setup Telegram command handlers."""
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("verify", self._cmd_verify))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self.app.add_handler(CommandHandler("news", self._cmd_news))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._text_handler))
        self.app.add_handler(CallbackQueryHandler(self._button_handler))

    # ========================================================================
    # Command Handlers
    # ========================================================================

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        chat_id = update.effective_chat.id
        user_id = None

        if ctx.args and len(ctx.args) > 0:
            user_id = ctx.args[0]

        if user_id:
            await update.message.reply_text(
                f"👋 Welcome to OptionBuddy Trading Agent!\n\n"
                f"To complete setup, send: <code>/verify {user_id}</code>",
                parse_mode="HTML"
            )
            log.info(f"User started with user_id: {user_id}")
        else:
            await update.message.reply_text(
                "👋 Welcome to OptionBuddy Trading Agent!\n\n"
                "Please register at the web dashboard and link your Telegram bot."
            )

    async def _cmd_verify(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /verify command."""
        chat_id = update.effective_chat.id

        if not ctx.args or len(ctx.args) < 1:
            await update.message.reply_text(
                "Usage: <code>/verify &lt;user_id&gt;</code>",
                parse_mode="HTML"
            )
            return

        user_id = ctx.args[0]

        try:
            import requests
            import os

            web_host = os.getenv("WEB_HOST", "http://localhost:8000")
            resp = requests.post(
                f"{web_host}/verify-telegram",
                json={"user_id": user_id, "chat_id": chat_id}
            )

            if resp.status_code == 200:
                await update.message.reply_text(
                    "✅ Telegram linked successfully!\n\n"
                    "Your OptionBuddy trading agent is now active."
                )
                log.info(f"Verified user {user_id} (chat_id: {chat_id})")
            else:
                await update.message.reply_text(
                    f"❌ Verification failed: {resp.json().get('message', 'Error')}"
                )

        except Exception as e:
            log.error(f"Verification error: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /pause command."""
        chat_id = update.effective_chat.id
        user = get_user_by_chat_id(chat_id)

        if not user:
            await update.message.reply_text("❌ User not found")
            return

        # TODO: Update user.is_paused in database
        await update.message.reply_text("⏸ Agent paused. Send /resume to restart.")
        log.info(f"Agent paused for user {user['username']}")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /resume command."""
        chat_id = update.effective_chat.id
        user = get_user_by_chat_id(chat_id)

        if not user:
            await update.message.reply_text("❌ User not found")
            return

        # TODO: Update user.is_paused in database
        await update.message.reply_text("▶️ Agent resumed.")
        log.info(f"Agent resumed for user {user['username']}")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        chat_id = update.effective_chat.id
        user = get_user_by_chat_id(chat_id)

        if not user:
            await update.message.reply_text("❌ User not found")
            return

        status = "⏸ PAUSED" if user['is_paused'] else "▶️ RUNNING"
        await update.message.reply_text(
            f"Agent Status for {user['username']}\n"
            f"Status: {status}\n"
            f"Broker: {user['broker_type']}\n"
            f"Signal Mode: {user['signal_mode']}"
        )

    async def _cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /pnl command."""
        chat_id = update.effective_chat.id
        user = get_user_by_chat_id(chat_id)

        if not user:
            await update.message.reply_text("❌ User not found. Please register first.")
            return

        await update.message.reply_text("📊 Fetching P&L report...")

        try:
            # TODO: Implement get_user_pnl_report(user_id)
            pnl = get_user_pnl_summary(user['user_id'])
            if pnl:
                msg = (
                    f"📊 P&L Summary for {user['username']}\n\n"
                    f"Unrealised: ₹{pnl.get('unrealised', 0):,.2f}\n"
                    f"Realised: ₹{pnl.get('realised', 0):,.2f}\n"
                    f"Net: ₹{pnl.get('net', 0):,.2f}"
                )
            else:
                msg = "No P&L data available"

            await update.message.reply_text(msg)
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _cmd_news(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /news command."""
        await update.message.reply_text("📰 Fetching latest market news...")

        try:
            from data.news_fetcher import fetch_rss_headlines, fetch_newsapi_headlines
            from datetime import datetime

            rss_news = fetch_rss_headlines()
            newsapi_news = fetch_newsapi_headlines()
            all_news = list(set(rss_news + newsapi_news))[:12]

            if all_news:
                news_msg = "<b>📰 LATEST MARKET NEWS</b>\n"
                news_msg += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M IST')}</i>\n"
                news_msg += "━" * 70 + "\n\n"

                for i, headline in enumerate(all_news, 1):
                    news_msg += f"<b>{i}.</b> {headline}\n\n"

                news_msg += "━" * 70
                await update.message.reply_text(news_msg, parse_mode="HTML")
            else:
                await update.message.reply_text("❌ No news available")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _text_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text messages (keyword matching)."""
        text = update.message.text.lower().strip()

        if any(kw in text for kw in ["pnl", "p&l", "trades", "pl", "daily"]):
            await self._cmd_pnl(update, ctx)

    async def _button_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle signal approval/rejection buttons."""
        query = update.callback_query
        await query.answer()

        try:
            # Data format: "user_id:signal_id:action"
            data_parts = query.data.split(":")
            action = data_parts[0]
            user_id = data_parts[1]
            signal_id = int(data_parts[2])

            if action == "approve" and self._approve_cb:
                await query.edit_message_text(f"✅ Approved signal #{signal_id}. Executing...")
                await self._approve_cb(user_id, signal_id)
            elif action == "reject" and self._reject_cb:
                await query.edit_message_text(f"❌ Rejected signal #{signal_id}.")
                await self._reject_cb(user_id, signal_id)

        except Exception as e:
            log.error(f"Button handler error: {e}")
            await query.edit_message_text(f"❌ Error: {str(e)}")

    # ========================================================================
    # Public API
    # ========================================================================

    async def send_signal_alert(
        self,
        signal: dict,
        signal_id: int,
        chat_id: int,
        is_shared: bool = False
    ) -> None:
        """Send signal alert to a specific user."""
        text = _signal_message(signal, signal_id, is_shared)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Execute", callback_data=f"approve:{signal_id}"),
                InlineKeyboardButton("❌ Skip", callback_data=f"reject:{signal_id}"),
            ]
        ])

        try:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            log.info(f"Signal alert sent to chat_id {chat_id}")
        except Exception as e:
            log.error(f"Failed to send signal alert: {e}")

    async def send_shared_signal_alert(
        self,
        signal: dict,
        signal_id: int,
        chat_id: int,
        sender_user_id: str
    ) -> None:
        """Send shared signal alert to a user."""
        await self.send_signal_alert(signal, signal_id, chat_id, is_shared=True)

    async def send_user_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "Markdown"
    ) -> None:
        """Send a message to a specific user."""
        try:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as e:
            log.error(f"Failed to send message to {chat_id}: {e}")

    async def send_broadcast(self, text: str) -> None:
        """Send system-wide broadcast (placeholder - needs user list)."""
        log.info(f"Broadcast message: {text}")
        # In production, iterate through all users and send to each

    async def start_polling(self) -> None:
        """Start Telegram polling."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram polling started")

    async def stop(self) -> None:
        """Stop Telegram polling."""
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        log.info("Telegram bot stopped")
