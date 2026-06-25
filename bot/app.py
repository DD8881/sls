from telegram.ext import Application, CommandHandler

import config
from bot.handlers import help_cmd, start


def create_app() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    return app
