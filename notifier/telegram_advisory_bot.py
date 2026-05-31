"""
telegram_advisory_bot.py — Advisory-only Telegram bot for research calls.

Delivers research calls and lifecycle updates to subscribers. There is NO order
execution and NO broker linking — this bot is purely informational/advisory.

Commands:
  /start       — subscribe to calls
  /stop        — pause receiving calls
  /calls       — show currently active calls
  /track       — show track record (accuracy / win rate)
  /categories  — toggle which call categories you receive
  /help        — usage help

Push API (called by the main advisory loop):
  push_new_call(call, call_id)   — broadcast a fresh call to subscribers of that category
  push_call_event(event)         — broadcast a target/SL/expiry update
  broadcast(text)                — broadcast plain message to all active subscribers
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config.settings import TELEGRAM_BOT_TOKEN
from config.logger import get_logger
from data.advisory_store import (
    CATEGORIES,
    add_subscriber,
    set_subscriber_active,
    set_subscriber_categories,
    get_subscriber,
    get_subscribers_for_category,
    get_active_calls,
    get_recent_calls,
    get_track_record,
)

log = get_logger("advisory_bot")

DISCLAIMER = (
    "⚠️ _Advisory only — not investment advice. Markets carry risk; "
    "trade at your own discretion._"
)

CATEGORY_LABEL = {
    "index_option": "📊 Index Options",
    "equity": "📈 Equity",
    "futures": "🔮 Futures",
    "commodity": "🛢 Commodity",
}

EVENT_LABEL = {
    "entry_triggered": "📍 Entry triggered",
    "target1_hit": "🎯 Target 1 hit",
    "target_hit": "✅ Target hit — call closed",
    "sl_hit": "🛑 Stop-loss hit — call closed",
    "expired": "⌛ Call expired",
}


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def format_call(call: dict, call_id: int) -> str:
    """Format a fresh call for Telegram (Markdown)."""
    action = str(call.get("action", "")).upper()
    arrow = "🟢" if action == "BUY" else "🔴"
    cat = CATEGORY_LABEL.get(call.get("category", ""), call.get("category", ""))
    tf = str(call.get("timeframe", "")).upper()
    conf = call.get("confidence", 0)

    emin, emax = call.get("entry_min"), call.get("entry_max")
    if emin is not None and emax is not None:
        entry_line = f"Entry: {_fmt_price(emin)} – {_fmt_price(emax)}"
    else:
        entry_line = f"Entry: {_fmt_price(call.get('entry_price'))}"

    lines = [
        f"{arrow} *{action}* — `{call.get('instrument', '?')}`  [{cat} · {tf}]",
        f"Confidence: *{conf}%*",
        "",
        entry_line,
        f"🎯 Target 1: {_fmt_price(call.get('target_1'))}",
    ]
    if call.get("target_2") is not None:
        lines.append(f"🎯 Target 2: {_fmt_price(call.get('target_2'))}")
    lines.append(f"🛑 Stop-loss: {_fmt_price(call.get('stop_loss'))}")

    if call.get("rationale"):
        lines += ["", f"📋 {call['rationale']}"]

    lines += ["", f"_Call #{call_id}_", DISCLAIMER]
    return "\n".join(lines)


def format_event(event: dict) -> str:
    """Format a lifecycle update for Telegram (Markdown)."""
    label = EVENT_LABEL.get(event["event_type"], event["event_type"])
    inst = event["instrument"]
    parts = [f"{label}\n`{inst}`  (Call #{event['call_id']})"]

    if event.get("result_pct") is not None:
        pct = event["result_pct"]
        sign = "🟢 +" if pct >= 0 else "🔴 "
        parts.append(f"Result: {sign}{pct:.2f}%")
    if event.get("price") is not None:
        parts.append(f"At: {_fmt_price(event['price'])}")
    return "\n".join(parts)


class TelegramAdvisoryBot:
    """Advisory-only Telegram bot with subscriber management and call push."""

    def __init__(self):
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CommandHandler("calls", self._cmd_calls))
        self.app.add_handler(CommandHandler("track", self._cmd_track))
        self.app.add_handler(CommandHandler("categories", self._cmd_categories))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CallbackQueryHandler(self._on_category_toggle, pattern=r"^cat:"))

    # ── Commands ────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        username = update.effective_user.username or update.effective_user.first_name or ""
        add_subscriber(chat_id, username)
        await update.message.reply_text(
            "👋 *Welcome to OptionBuddy Advisory!*\n\n"
            "You'll now receive research-backed trade calls with entry, target & stop-loss "
            "for Index Options, Equity, Futures and Commodity.\n\n"
            "• /calls — active calls\n"
            "• /track — our track record\n"
            "• /categories — choose what you receive\n"
            "• /stop — pause calls\n\n"
            + DISCLAIMER,
            parse_mode="Markdown",
        )
        log.info("New subscriber via /start: %s (%s)", chat_id, username)

    async def _cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        set_subscriber_active(update.effective_chat.id, False)
        await update.message.reply_text(
            "⏸ You've paused calls. Send /start anytime to resume."
        )

    async def _cmd_calls(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        active = get_active_calls()
        if not active:
            await update.message.reply_text(
                "No active calls right now. We'll alert you the moment a fresh setup appears."
            )
            return
        await update.message.reply_text(f"📋 *{len(active)} active call(s):*", parse_mode="Markdown")
        for call in active[:10]:
            await update.message.reply_text(
                format_call(call, call["id"]), parse_mode="Markdown"
            )

    async def _cmd_track(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rec = get_track_record(days=30)
        o = rec["overall"]
        msg = [
            "📊 *Track Record (last 30 days)*",
            "",
            f"Total calls closed: *{o['total']}*",
            f"Wins: *{o['wins']}*  |  Losses: *{o['losses']}*",
            f"Win rate: *{o['win_rate']}%*",
            f"Avg return / call: *{o['avg_return']}%*",
            "",
            "*By category:*",
        ]
        for cat, s in rec["by_category"].items():
            if s["total"]:
                msg.append(
                    f"{CATEGORY_LABEL.get(cat, cat)}: {s['win_rate']}% "
                    f"({s['wins']}/{s['total']}), avg {s['avg_return']}%"
                )
        msg += ["", DISCLAIMER]
        await update.message.reply_text("\n".join(msg), parse_mode="Markdown")

    async def _cmd_categories(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        sub = get_subscriber(update.effective_chat.id)
        if not sub:
            add_subscriber(update.effective_chat.id,
                            update.effective_user.username or "")
            sub = get_subscriber(update.effective_chat.id)
        await update.message.reply_text(
            "Tap to toggle the categories you want to receive:",
            reply_markup=self._category_keyboard(sub.get("categories", "")),
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "*OptionBuddy Advisory — Help*\n\n"
            "We send research-backed trade calls with clear entry, target & stop-loss, "
            "then alert you when a target or stop-loss is hit.\n\n"
            "• /calls — currently active calls\n"
            "• /track — accuracy & win rate\n"
            "• /categories — choose Index Options / Equity / Futures / Commodity\n"
            "• /stop — pause  •  /start — resume\n\n"
            + DISCLAIMER,
            parse_mode="Markdown",
        )

    # ── Category toggle keyboard ──────────────────────────────────────────────

    def _category_keyboard(self, current_csv: str) -> InlineKeyboardMarkup:
        current = set(c for c in (current_csv or "").split(",") if c)
        rows = []
        for cat in CATEGORIES:
            on = cat in current
            mark = "✅" if on else "⬜"
            rows.append([InlineKeyboardButton(
                f"{mark} {CATEGORY_LABEL.get(cat, cat)}",
                callback_data=f"cat:{cat}",
            )])
        return InlineKeyboardMarkup(rows)

    async def _on_category_toggle(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat.id
        cat = query.data.split(":", 1)[1]

        sub = get_subscriber(chat_id)
        current = set(c for c in (sub.get("categories", "") if sub else "").split(",") if c)
        if cat in current:
            current.discard(cat)
        else:
            current.add(cat)
        # Preserve canonical ordering
        ordered = [c for c in CATEGORIES if c in current]
        set_subscriber_categories(chat_id, ordered)

        await query.edit_message_reply_markup(
            reply_markup=self._category_keyboard(",".join(ordered))
        )

    # ── Push API ──────────────────────────────────────────────────────────────

    async def push_new_call(self, call: dict, call_id: int) -> None:
        """Broadcast a fresh call to all subscribers of its category."""
        text = format_call(call, call_id)
        recipients = get_subscribers_for_category(call.get("category", ""))
        sent = 0
        for sub in recipients:
            try:
                await self.app.bot.send_message(
                    chat_id=sub["chat_id"], text=text, parse_mode="Markdown"
                )
                sent += 1
            except Exception as e:
                log.warning("Failed to push call to %s: %s", sub["chat_id"], e)
        log.info("Call #%s pushed to %s subscribers (%s)", call_id, sent, call.get("category"))

    async def push_call_event(self, event: dict) -> None:
        """Broadcast a lifecycle update (target/SL/expiry) to subscribers of the category."""
        text = format_event(event)
        recipients = get_subscribers_for_category(event.get("category", ""))
        for sub in recipients:
            try:
                await self.app.bot.send_message(
                    chat_id=sub["chat_id"], text=text, parse_mode="Markdown"
                )
            except Exception as e:
                log.warning("Failed to push event to %s: %s", sub["chat_id"], e)

    async def broadcast(self, text: str, parse_mode: str = "Markdown") -> None:
        """Broadcast a plain message to all active subscribers."""
        from data.advisory_store import get_all_active_subscribers
        for sub in get_all_active_subscribers():
            try:
                await self.app.bot.send_message(
                    chat_id=sub["chat_id"], text=text, parse_mode=parse_mode
                )
            except Exception as e:
                log.warning("Failed to broadcast to %s: %s", sub["chat_id"], e)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start_polling(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Advisory Telegram bot polling started")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        log.info("Advisory Telegram bot stopped")
