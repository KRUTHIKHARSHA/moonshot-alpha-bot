"""
core/main.py
------------
Application entry point.

Responsibilities:
  - Load environment variables
  - Initialise Firebase
  - Register Telegram bot handlers
  - Start background scheduler
  - Run bot polling
"""

import asyncio
import logging
import os
from threading import Thread

from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from database.firebase import init_firebase
from core.bot_handlers import button_handler, handle_text, start
from core.scheduler import background_scheduler

load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask health-check server (keeps Render/Railway alive)
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "MoonshotAlpha is running 🚀", 200


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.critical("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        return

    if not init_firebase():
        logger.critical("Firebase failed to initialise. Exiting.")
        return

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))

    # Callbacks
    app.add_handler(CallbackQueryHandler(button_handler))

    # Text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Post-init: kick off background tasks
    async def post_init(application: Application) -> None:
        asyncio.create_task(background_scheduler())
        admin_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
        if admin_id:
            await application.bot.send_message(
                chat_id=admin_id,
                text="🚀 MoonshotAlpha Bot is online!",
            )

    app.post_init = post_init

    # Start Flask in a daemon thread
    port = int(os.getenv("PORT", 8080))
    Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port),
        daemon=True,
    ).start()

    logger.info("Bot polling started on port %d…", port)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()