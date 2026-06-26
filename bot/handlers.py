from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import config


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if config.WEBAPP_URL:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Відкрити знижки", web_app=WebAppInfo(url=config.WEBAPP_URL))],
        ])
        await update.message.reply_text(
            "Натисніть кнопку щоб переглянути знижки:",
            reply_markup=keyboard,
        )
        return
    await update.message.reply_text("Налаштуйте WEBAPP_URL для використання бота.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛒 *Бот знижок у супермаркетах*\n\n"
        "/start — відкрити застосунок\n"
        "Пошук товарів — усередині застосунку\\."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
