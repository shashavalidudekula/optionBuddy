"""
telegram_bot.py — Telegram alerts with ✅/❌ approval buttons + /pause /resume commands

Usage:
  bot = TelegramBot()
  await bot.send_signal_alert(signal, signal_id)  # sends alert with buttons
  # When user taps ✅, on_approve(signal_id) is called
  # When user taps ❌, on_reject(signal_id) is called
"""
import asyncio
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

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from config.logger import get_logger

log = get_logger("telegram_bot")

# ── Callbacks registered by main.py ───────────────────────────────────────────
ApproveCallback = Callable[[int], Awaitable[None]]
RejectCallback  = Callable[[int], Awaitable[None]]


def _signal_message(signal: dict, signal_id: int) -> str:
    sig   = signal.get("signal", "?")
    inst  = signal.get("instrument", "?")
    conf  = signal.get("confidence", 0)
    urg   = signal.get("urgency", "?").upper()
    reason = signal.get("reason", "")
    max_loss = signal.get("max_loss_if_held", 0)
    benefit  = signal.get("action_benefit", 0)
    risk     = signal.get("key_risk", "")
    watch    = signal.get("watch_level", "")

    urg_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(urg, "⚪")

    return (
        f"{urg_emoji} *{sig}* — `{inst}`\n"
        f"Confidence: *{conf}%* | Urgency: *{urg}*\n\n"
        f"📋 {reason}\n\n"
        f"📉 Max loss if held: ₹{max_loss:,.0f}\n"
        f"✅ Benefit of action: ₹{benefit:,.0f}\n"
        f"⚠️ Key risk: {risk}\n"
        f"👁 Watch: {watch}\n\n"
        f"_Signal ID: {signal_id}_"
    )


class TelegramBot:
    def __init__(self):
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self._paused = False
        self._approve_cb: ApproveCallback | None = None
        self._reject_cb: RejectCallback | None = None
        self._setup_handlers()

    def register_callbacks(
        self,
        on_approve: ApproveCallback,
        on_reject: RejectCallback,
    ) -> None:
        self._approve_cb = on_approve
        self._reject_cb = on_reject

    def _setup_handlers(self) -> None:
        self.app.add_handler(CommandHandler("pause",  self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("pnl",    self._cmd_pnl))
        self.app.add_handler(CommandHandler("news",   self._cmd_news))
        self.app.add_handler(CommandHandler("testnews", self._cmd_testnews))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._text_handler))
        self.app.add_handler(CallbackQueryHandler(self._button_handler))

    # ── Commands ───────────────────────────────────────────────────────────────

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            return
        self._paused = True
        await update.message.reply_text("⏸ Agent paused. Send /resume to restart.")
        log.info("Agent paused via Telegram.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            return
        self._paused = False
        await update.message.reply_text("▶️ Agent resumed.")
        log.info("Agent resumed via Telegram.")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            return
        state = "⏸ PAUSED" if self._paused else "▶️ RUNNING"
        await update.message.reply_text(f"Agent status: {state}")

    async def _cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /pnl command — show daily P&L report."""
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            return
        await update.message.reply_text("📊 Fetching P&L report...")
        pnl_report = await self._fetch_pnl_report()
        await update.message.reply_text(pnl_report, parse_mode="HTML")

    async def _cmd_news(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /news command — fetch latest market news."""
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            return
        await update.message.reply_text("📰 Fetching latest market news...")
        news_report = await self._fetch_latest_news()
        await update.message.reply_text(news_report, parse_mode="HTML")

    async def _cmd_testnews(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /testnews command — test 8 AM news functionality."""
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            return
        await update.message.reply_text("🧪 Testing 8 AM news flow...")
        try:
            from data.news_fetcher import fetch_rss_headlines, fetch_newsapi_headlines
            from datetime import datetime

            rss_news = fetch_rss_headlines()
            newsapi_news = fetch_newsapi_headlines()
            all_news = list(set(rss_news + newsapi_news))[:12]

            if all_news:
                news_msg = "<b>📰 TEST: 8 AM MARKET NEWS</b>\n"
                news_msg += f"<i>Test sent at {datetime.now().strftime('%Y-%m-%d %H:%M IST')}</i>\n"
                news_msg += "━" * 70 + "\n\n"
                for i, headline in enumerate(all_news, 1):
                    news_msg += f"<b>{i}.</b> {headline}\n\n"
                news_msg += "━" * 70 + f"\n<i>Total: {len(all_news)} headlines</i>"
                await update.message.reply_text(news_msg, parse_mode="HTML")
            else:
                await update.message.reply_text("❌ No news available")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _text_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text messages — respond to keywords like 'pnl', 'trades', 'p&l'."""
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            return

        text = update.message.text.lower().strip()
        pnl_keywords = ["pnl", "p&l", "trades", "pl", "daily"]

        if any(kw in text for kw in pnl_keywords):
            await update.message.reply_text("📊 Fetching P&L report...")
            pnl_report = await self._fetch_pnl_report()
            await update.message.reply_text(pnl_report, parse_mode="HTML")

    async def _fetch_latest_news(self) -> str:
        """Fetch and format latest market news for Telegram."""
        try:
            from data.news_fetcher import fetch_rss_headlines, fetch_newsapi_headlines
            from datetime import datetime

            # Fetch from both RSS and NewsAPI
            rss_news = fetch_rss_headlines()
            newsapi_news = fetch_newsapi_headlines()

            # Combine and deduplicate
            all_news = list(set(rss_news + newsapi_news))[:12]  # Top 12 unique headlines

            if not all_news:
                return "<b>📰 No market news available at the moment.</b>"

            report = "<b>📰 LATEST MARKET NEWS</b>\n"
            report += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M IST')}</i>\n"
            report += "━" * 70 + "\n\n"

            for i, headline in enumerate(all_news, 1):
                # Show full headlines without truncation
                report += f"<b>{i}.</b> {headline}\n\n"

            report += "━" * 70 + "\n"
            report += f"<i>Total headlines: {len(all_news)}</i>"

            return report

        except Exception as e:
            log.error("Error fetching news: %s", e)
            return f"<b>Error fetching news:</b> {str(e)}"

    async def _fetch_pnl_report(self) -> str:
        """Fetch P&L data and format as table for Telegram."""
        try:
            from core.indstocks_auth import get_session
            session = get_session()

            resp = session.get("/portfolio/positions", params={"segment": "derivative", "product": "margin"})

            if isinstance(resp, list):
                positions = resp
            else:
                data = resp.get("data", [])
                if isinstance(data, dict):
                    positions = data.get("net_positions", [])
                elif isinstance(data, list):
                    positions = data
                else:
                    positions = []

            trades = []
            pending_positions = []
            total_pnl = 0
            win_count = 0

            for p in positions:
                net_qty = int(p.get("net_qty", 0))

                # Collect pending/open positions (net_qty != 0)
                if net_qty != 0:
                    symbol = p.get("symbol", "")
                    strike = p.get("drv_strike_price", "")
                    opt_type = p.get("drv_option_type", "")

                    if strike and opt_type:
                        full_symbol = f"{symbol}{strike}{opt_type}"
                    else:
                        full_symbol = symbol

                    avg_price = float(p.get("avg_price", 0))
                    direction = "LONG" if net_qty > 0 else "SHORT"

                    pending_positions.append({
                        "symbol": full_symbol,
                        "direction": direction,
                        "qty": abs(net_qty),
                        "avg_price": avg_price
                    })

                elif net_qty == 0:
                    symbol = p.get("symbol", "")
                    strike = p.get("drv_strike_price", "")
                    opt_type = p.get("drv_option_type", "")

                    if strike and opt_type:
                        full_symbol = f"{symbol}{strike}{opt_type}"
                    else:
                        full_symbol = symbol

                    buy_avg = float(p.get("buy_avg", 0))
                    sell_avg = float(p.get("sell_avg", 0))
                    realized_pnl = float(p.get("realized_profit", 0))

                    trades.append({
                        "symbol": full_symbol,
                        "buy_avg": buy_avg,
                        "sell_avg": sell_avg,
                        "pnl": realized_pnl
                    })

                    total_pnl += realized_pnl
                    if realized_pnl > 0:
                        win_count += 1

            # Format as HTML table for Telegram with color coding
            if pending_positions:
                report = "<b>⏳ PENDING/OPEN POSITIONS</b>\n"
                report += "━" * 65 + "\n"
                for pos in pending_positions:
                    emoji = "📈" if pos['direction'] == "LONG" else "📉"
                    report += f"  {emoji} {pos['symbol']:<18} {pos['direction']:<8} Qty: <b>{pos['qty']}</b>\n"
                    report += f"    Entry Price: ₹{pos['avg_price']:.2f}\n"
                report += "\n"
            else:
                report = ""

            if trades:
                report = "<b>📊 TODAY'S CLOSED TRADES</b>\n"
                report += "━" * 65 + "\n\n"

                profit_trades = []
                loss_trades = []

                for t in trades:
                    if t['pnl'] > 0:
                        profit_trades.append(t)
                    else:
                        loss_trades.append(t)

                # Show profit trades first with green emoji
                if profit_trades:
                    report += "<b>🟢 PROFIT TRADES</b>\n"
                    report += "─" * 65 + "\n"
                    for t in profit_trades:
                        report += f"  {t['symbol']:<18} → <b>₹{t['pnl']:>10,.0f}</b> ✅\n"
                        report += f"    Buy: {t['buy_avg']:>8.2f} | Sell: {t['sell_avg']:>8.2f}\n"
                    report += "\n"

                # Show loss trades with red emoji
                if loss_trades:
                    report += "<b>🔴 LOSS TRADES</b>\n"
                    report += "─" * 65 + "\n"
                    for t in loss_trades:
                        report += f"  {t['symbol']:<18} → <b>₹{t['pnl']:>10,.0f}</b> ❌\n"
                        report += f"    Buy: {t['buy_avg']:>8.2f} | Sell: {t['sell_avg']:>8.2f}\n"
                    report += "\n"

                # Summary
                report += "━" * 65 + "\n"
                report += f"<b>📈 Summary:</b>\n"
                report += f"  Total Trades: <b>{len(trades)}</b>\n"
                report += f"  Profit Trades: <b>{win_count}</b> 🟢\n"
                report += f"  Loss Trades: <b>{len(trades) - win_count}</b> 🔴\n"

                pnl_color = "🟢" if total_pnl >= 0 else "🔴"
                report += f"  Net P&L: {pnl_color} <b>₹{total_pnl:,.2f}</b>\n"
                report += f"  Win Rate: <b>{win_count}/{len(trades)} ({100*win_count//len(trades) if trades else 0}%)</b>\n"
                report += "━" * 65
            elif not pending_positions:
                report += "<b>ℹ️ No trades or positions found.</b>"

            return report

        except Exception as e:
            log.error("Error fetching P&L report: %s", e)
            return f"<b>Error fetching P&L:</b> {str(e)}"

    # ── Button handler ─────────────────────────────────────────────────────────

    async def _button_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        data = query.data  # format: "approve:42" or "reject:42"
        action, signal_id_str = data.split(":", 1)
        signal_id = int(signal_id_str)

        if action == "approve" and self._approve_cb:
            await query.edit_message_text(f"✅ Approved signal #{signal_id}. Executing...")
            await self._approve_cb(signal_id)
        elif action == "reject" and self._reject_cb:
            await query.edit_message_text(f"❌ Rejected signal #{signal_id}.")
            if self._reject_cb:
                await self._reject_cb(signal_id)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def send_signal_alert(self, signal: dict, signal_id: int) -> None:
        """Send a signal alert with ✅/❌ buttons."""
        if self._paused:
            log.info("Alert suppressed — agent paused.")
            return

        text = _signal_message(signal, signal_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Execute", callback_data=f"approve:{signal_id}"),
                InlineKeyboardButton("❌ Skip",    callback_data=f"reject:{signal_id}"),
            ]
        ])
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            log.info("Alert sent for signal #%d", signal_id)
        except Exception as e:
            log.error("Failed to send Telegram alert: %s", e)

    async def send_message(self, text: str) -> None:
        """Send a plain text message (status updates, errors, P&L reports)."""
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error("Failed to send Telegram message: %s", e)

    async def send_daily_summary(self, realised: float, unrealised: float, positions: list) -> None:
        net = realised + unrealised
        sign = "🟢" if net >= 0 else "🔴"
        pos_lines = "\n".join(
            f"  • {p['symbol']}: ₹{p['pnl']:,.0f}" for p in positions
        ) or "  (none)"

        msg = (
            f"📊 *Daily Summary*\n\n"
            f"Realised P&L:   ₹{realised:,.0f}\n"
            f"Unrealised P&L: ₹{unrealised:,.0f}\n"
            f"{sign} Net: ₹{net:,.0f}\n\n"
            f"Open positions:\n{pos_lines}"
        )
        await self.send_message(msg)

    async def send_morning_briefing(self, briefing_text: str) -> None:
        """Send morning market briefing at 8 AM."""
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=briefing_text,
                parse_mode="HTML",
            )
            log.info("Morning briefing sent")
        except Exception as e:
            log.error("Failed to send morning briefing: %s", e)

    async def send_midday_briefing(self, briefing_text: str) -> None:
        """Send mid-day market update at 11:30 AM."""
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=briefing_text,
                parse_mode="HTML",
            )
            log.info("Mid-day briefing sent")
        except Exception as e:
            log.error("Failed to send mid-day briefing: %s", e)

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def start_polling(self) -> None:
        """Start receiving updates (non-blocking — runs in background)."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram polling started.")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        log.info("Telegram bot stopped.")
