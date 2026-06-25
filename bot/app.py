from telegram.ext import Application, CallbackQueryHandler, CommandHandler

import config
from bot.handlers import button_callback, help_cmd, search_cmd, start, stats_cmd


def create_app() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    return app
