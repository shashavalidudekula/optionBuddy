"""
send_test_notification.py — dev helper to verify Telegram delivery.

Two modes:

  # 1) Quick check — send to ONE chat_id directly (no DB, works any time):
  python send_test_notification.py 123456789

  # 2) Real path — send to ALL active subscribers (needs Postgres up and at
  #    least one /start). Run this inside the container:
  python send_test_notification.py

Get your chat_id: message your bot once, then open
  https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
and read result[].message.chat.id  (or message @userinfobot on Telegram).
"""
import asyncio
import sys

from telegram import Bot

from config.settings import TELEGRAM_BOT_TOKEN

SAMPLE_TEXT = (
    "🟢 *BUY* — `NIFTY 24500 CE`  [📊 Index Options · INTRADAY]\n"
    "Confidence: *82%*\n\n"
    "Entry: ₹115.00 – ₹125.00\n"
    "🎯 Target 1: ₹150.00\n"
    "🎯 Target 2: ₹180.00\n"
    "🛑 Stop-loss: ₹95.00\n\n"
    "📋 Test call — verifying Telegram delivery.\n\n"
    "_Call #test_\n"
    "⚠️ _Advisory only — not investment advice._"
)


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set in .env"); return

    bot = Bot(TELEGRAM_BOT_TOKEN)
    me = await bot.get_me()
    print(f"Bot OK: @{me.username}")

    if len(sys.argv) > 1:
        chat_ids = [int(sys.argv[1])]
    else:
        # DB import is lazy so the quick (chat_id) mode needs no psycopg2.
        from data.advisory_store import get_all_active_subscribers
        chat_ids = [s["chat_id"] for s in get_all_active_subscribers()]

    if not chat_ids:
        print("No recipients. Send /start to the bot first, or pass a chat_id argument.")
        return

    for cid in chat_ids:
        try:
            await bot.send_message(chat_id=cid, text=SAMPLE_TEXT, parse_mode="Markdown")
            print(f"sent → {cid}")
        except Exception as e:
            print(f"FAILED → {cid}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
