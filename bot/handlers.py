from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import config


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if config.WEBAPP_URL:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Відкрити знижки", web_app=WebAppInfo(url=config.WEBAPP_URL))],
        ])
        first = (update.effective_user.first_name or "").replace("<", "").replace(">", "").replace("&", "")
        hi = f"👋 Вітаємо, {first}!" if first else "👋 Вітаємо!"
        await update.message.reply_text(
            f"{hi}\n\n"
            "🛒 <b>Sales UA</b> — усі знижки супермаркетів України в одному застосунку.\n\n"
            "📍 Ваше місто й найближчі магазини\n"
            "🔍 Пошук товарів одразу за всіма категоріями\n\n"
            "Тисніть кнопку нижче, щоб почати 👇",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
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
