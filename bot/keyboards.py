from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config


CHAIN_LABELS = {
    "silpo": "Silpo",
    "novus": "Novus",
    "metro": "Metro",
}


def store_keyboard(chains: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for ch in chains:
        chain = ch["chain"]
        label = CHAIN_LABELS.get(chain, chain.capitalize())
        buttons.append(
            InlineKeyboardButton(f"{label} ({ch['cnt']})", callback_data=f"s:{chain}")
        )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("Всі магазини", callback_data="s:all")])
    return InlineKeyboardMarkup(rows)


def category_keyboard(chain: str, categories: list[dict], page: int) -> InlineKeyboardMarkup:
    per_page = config.CATEGORIES_PER_PAGE
    start = page * per_page
    end = start + per_page
    page_cats = categories[start:end]
    total_pages = (len(categories) + per_page - 1) // per_page

    rows = []
    for i in range(0, len(page_cats), 2):
        row = []
        for cat in page_cats[i : i + 2]:
            label = f"{cat['title']} ({cat['cnt']})"
            if len(label) > 30:
                label = cat["title"][:27] + f"… ({cat['cnt']})"
            row.append(InlineKeyboardButton(label, callback_data=f"c:{chain}:{cat['slug']}:0"))
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"cl:{chain}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"cl:{chain}:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("↩️ Магазини", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)


def product_navigation(chain: str, cat_slug: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"c:{chain}:{cat_slug}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"c:{chain}:{cat_slug}:{page + 1}"))
    return InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton("↩️ Категорії", callback_data=f"cl:{chain}:0")],
    ])
