import os
import nest_asyncio
import asyncio
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler
from bot import (  # استورد كل الدوال والـ handlers من bot.py
    start,
    deposit_cmd,
    confirm_cmd,
    balance_cmd,
    withdraw_cmd,
    withdraw_profits_cmd,
    button_handler
)

# ========== ENV variables ==========
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # الرابط العام الذي يوفره Cloud Run
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise RuntimeError("TOKEN env var not set")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL env var not set")

# ========== Main async function ==========
async def main():
    # تطبيق البوت
    app = ApplicationBuilder().token(TOKEN).build()

    # تسجيل Handlers بشكل صحيح
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("confirm", confirm_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))
    app.add_handler(CommandHandler("withdraw_profits", withdraw_profits_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    # ضبط الـ webhook
    await app.bot.set_webhook(WEBHOOK_URL)

    print(f"🤖 البوت شغال على Cloud Run مع webhook: {WEBHOOK_URL}")

    # تشغيل webhook server
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL
    )

# ========== Run ==========
if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())
