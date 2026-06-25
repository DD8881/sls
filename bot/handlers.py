import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db
from bot.keyboards import CHAIN_LABELS
import config


def _escape_md(text: str) -> str:
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in text)


def _format_product(p: dict) -> str:
    title = _escape_md(p["title"])
    price = f"{p['price']:.2f}"
    chain_label = CHAIN_LABELS.get(p["chain"], p["chain"].capitalize())

    lines = [f"*{title}*"]
    if p.get("old_price"):
        old = f"{p['old_price']:.2f}"
        disc = f"-{p['discount_pct']:.0f}%" if p.get("discount_pct") else ""
        lines.append(f"~{_escape_md(old)}~ → *{_escape_md(price)} грн* {_escape_md(disc)}")
    else:
        lines.append(f"*{_escape_md(price)} грн*")

    meta = f"🏪 {_escape_md(chain_label)}"
    if p.get("promo_end_date"):
        meta += f" \\| 📅 до {_escape_md(p['promo_end_date'])}"
    lines.append(meta)
    return "\n".join(lines)


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
        "/search _запит_ — пошук товару\n"
        "/stats — статистика бази"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Використання: /search <запит>\nНаприклад: /search молоко")
        return
    conn = db.get_connection()
    products, _ = db.search_products(conn, query, limit=10)
    conn.close()
    if not products:
        await update.message.reply_text(f"Нічого не знайдено за запитом «{query}».")
        return
    text = f"🔍 *Результати для* «{_escape_md(query)}»:\n\n"
    text += "\n\n".join(_format_product(p) for p in products)
    if len(text) > 4000:
        text = text[:4000] + "\n\\.\\.\\."
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db.get_connection()
    stats = db.get_stats(conn)
    conn.close()
    lines = [f"📊 *Статистика*\n"]
    lines.append(f"Всього товарів: *{stats['total_products']}*")
    lines.append(f"Магазинів: *{stats['total_stores']}*")
    for chain, cnt in stats["chains"].items():
        label = CHAIN_LABELS.get(chain, chain.capitalize())
        lines.append(f"  {_escape_md(label)}: {cnt}")
    if stats["last_update"]:
        lines.append(f"\nОновлено: {_escape_md(stats['last_update'][:16])}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
