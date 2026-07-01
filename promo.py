#!/usr/bin/env python3
"""Генератор промо-банерів (PNG) з топ-знижками — для каналу/соцмереж.

Запуск локально на вимогу:
    ./.venv/bin/python promo.py                       # на свіжій discounts.db
    ./.venv/bin/python promo.py --db discounts.db.bak --out promo_draft

Виходять ДВА банери (4:5, 1080×1350):
    <out>_pct.png   — найвигідніші % серед їжі/FMCG
    <out>_save.png  — найбільша економія грн (5 їжа + 1 «велика покупка»-гачок)

Логіка (узгоджено):
  • охоплення — вся Україна (дедуп за товаром, беремо найкращу пропозицію);
  • фокус — гібрид: їжа/FMCG + 1 техніка-гачок; алкоголь і тютюн виключені;
  • фільтри якості відсікають накручені % і копійчаний дріб'язок.
Текст PNG — лише Arial (кирилиця ок). Валюта — словом «грн» (символ ₴ Arial не має).
"""
from __future__ import annotations

import argparse
import html
import io
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ── категорії ────────────────────────────────────────────────────────────────
FOOD = {
    "dairy", "meat", "fish", "fruits", "bakery", "grocery", "frozen", "canned",
    "sauces", "sweets", "snacks", "drinks", "hot_drinks", "hygiene", "household",
}
# для топу за економією-грн виключаємо hygiene: туди разом із шампунями падає
# дорога електро-гігієна (зубні щітки, епілятори, бритви), яка з'їдає весь топ ₴
FOOD_SAVE = FOOD - {"hygiene"}
HOOK = {"home", "kitchen", "hobby", "kids", "pets", "health"}  # сюди падає техніка

# ── шрифти ───────────────────────────────────────────────────────────────────
FONT_DIR = "/System/Library/Fonts/Supplemental/"
def _f(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_DIR + name, size)

# ── палітра ──────────────────────────────────────────────────────────────────
BG     = (247, 247, 251)
CARD   = (255, 255, 255)
INK    = (17, 18, 22)
MUTED  = (140, 144, 154)
RED    = (225, 29, 42)      # бейдж знижки / перекреслення старої ціни
GREEN  = (22, 163, 74)      # нова ціна
ACCENT = (240, 70, 50)      # бренд-акцент (коралово-червоний, як цінник у лого)
SHADOW = (221, 223, 231)

CHAIN_LABEL = {
    "silpo": "Сільпо", "atb": "АТБ", "auchan": "Ашан", "metro": "Metro",
    "novus": "Novus", "varus": "Varus", "fora": "Fora", "fozzy": "Fozzy",
}

# ── вибірка з БД ──────────────────────────────────────────────────────────────
# ── covering-індекси: запит читається цілком з індексу, без rowid-lookup'ів по
#    1GB БД. Без них планувальник тягне «товар×магазин» (Silpo — 442 копії товару)
#    і робить TEMP B-TREE на мільйонах рядків → хвилини. З ними — ~0.1с.
COVER_DISC = "idx_sp_cover"       # (discount_pct, in_stock, price, old_price, product_id)
COVER_SAVE = "idx_sp_cover_save"  # ((old_price-price), in_stock, price, old_price, discount_pct, product_id)
SCAN = 40000      # скільки рядків зняти з вершини (covering → дешево)
MAX_PIDS = 2500   # стільки унікальних товарів передивитись під фільтр категорій


def ensure_indexes(db):
    """Разова побудова (~по 65с на 5M рядків); далі IF NOT EXISTS — миттєвий no-op.
    Перебудовувати не треба: скрап оновлює рядки, індекси підтримуються самі."""
    db.execute(f"CREATE INDEX IF NOT EXISTS {COVER_DISC} ON "
               "store_products(discount_pct, in_stock, price, old_price, product_id)")
    db.execute(f"CREATE INDEX IF NOT EXISTS {COVER_SAVE} ON "
               "store_products((old_price-price), in_stock, price, old_price, "
               "discount_pct, product_id)")
    db.commit()


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_top(db, *, idx, order_expr, where, params, cats, want):
    """1) топ store_products за метрикою через covering-індекс (без join, ~0.1с);
    2) дедуп за товаром у порядку метрики (нацмережі дублюють товар по магазинах);
    3) добір категорій/назв із products батчем; 4) фільтр потрібних категорій +
    дедуп за назвою → топ `want` унікальних товарів."""
    rows = db.execute(
        f"""select product_id, price, old_price, discount_pct
            from store_products indexed by {idx}
            where in_stock=1 and old_price>price {where}
            order by {order_expr} desc limit ?""", [*params, SCAN]).fetchall()

    best, order_pids = {}, []
    for pid, price, old, disc in rows:
        if pid not in best:
            best[pid] = (price, old, disc)
            order_pids.append(pid)
            if len(order_pids) >= MAX_PIDS:
                break

    meta = {}
    for ch in _chunks(order_pids, 900):
        qm = ",".join("?" * len(ch))
        for pid, title, chain, img, cat in db.execute(
                f"select id, title, chain, image_url, unified_category "
                f"from products where id in ({qm})", ch):
            meta[pid] = (title, chain, img, cat)

    out, seen, brands = [], set(), set()
    for pid in order_pids:
        m = meta.get(pid)
        if not m:
            continue
        title, chain, img, cat = m
        if cat not in cats:
            continue
        title = html.unescape(title)        # &amp; &quot; → & "
        nm = " ".join(title.lower().split())[:40]
        if nm in seen:
            continue
        # дедуп за брендом: перше латинське слово (Finish/Persil/Lavazza…), щоб не
        # було 3× той самий бренд поспіль; товари без латиниці не дедупимо
        bm = re.search(r"[A-Za-z][A-Za-z&'\-]{2,}", title)
        bk = bm.group().lower() if bm else None
        if bk and bk in brands:
            continue
        seen.add(nm)
        if bk:
            brands.add(bk)
        price, old, disc = best[pid]
        out.append(dict(pid=pid, title=title, chain=chain, image_url=img,
                        price=price, old_price=old, discount_pct=disc))
        if len(out) >= want:
            break
    return out


# ── завантаження фото ────────────────────────────────────────────────────────
def load_img(url: str, size: int):
    try:
        if not url or not url.startswith("http"):
            return None
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        return ImageOps.fit(im, (size, size), Image.LANCZOS)
    except Exception:
        return None


def prefetch(rows, size):
    with ThreadPoolExecutor(max_workers=8) as ex:
        imgs = list(ex.map(lambda r: load_img(r["image_url"], size), rows))
    for r, im in zip(rows, imgs):
        r["_img"] = im
    return rows


# ── примітиви рендеру ─────────────────────────────────────────────────────────
def rounded(draw, box, rad, fill):
    draw.rounded_rectangle(box, radius=rad, fill=fill)


TG_BLUE = (41, 169, 235)   # фірмовий блакитний Telegram
def telegram_icon(size, blue=TG_BLUE):
    """Іконка Telegram (блакитне коло + білий паперовий літачок), RGBA.
    Малюємо у 4× і зменшуємо — щоб краї були згладжені (PIL не антиаліасить полігони)."""
    ss = 4
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = S / 2
    d.ellipse((0, 0, S - 1, S - 1), fill=blue + (255,))
    P = lambda px, py: (r + px * r, r + py * r)       # координати від центру, одиниця = радіус
    A = P(0.60, -0.40)    # ніс (верх-право)
    B = P(-0.60, -0.06)   # хвіст (верх-ліво)
    C = P(-0.10, 0.10)    # злам крила
    D = P(0.04, 0.42)     # нижній кінчик хвостового пера
    E = P(0.16, 0.18)     # черевце
    d.polygon([A, B, C, D, E], fill=(255, 255, 255, 255))
    d.polygon([A, E, C], fill=(214, 226, 234, 255))  # тінь згину
    return img.resize((size, size), Image.LANCZOS)


def wrap2(draw, text, font, max_w):
    """Назва максимум у 2 рядки; якщо хвіст не влазить — «…» на останньому."""
    words, lines, cur, i = text.split(), [], "", 0
    while i < len(words) and len(lines) < 2:
        t = (cur + " " + words[i]).strip()
        if not cur or draw.textlength(t, font=font) <= max_w:
            cur = t
            i += 1
        else:
            lines.append(cur)
            cur = ""
    if cur and len(lines) < 2:
        lines.append(cur)
        i = len(words)
    if i < len(words) and lines:                      # лишилися невлізлі слова
        s = lines[-1]
        while s and draw.textlength(s + " …", font=font) > max_w:
            s = s[:-1]
        lines[-1] = s.rstrip() + "…"
    return lines[:2]


def money(v: float) -> str:
    return f"{v:.0f}" if v == int(v) else f"{v:.2f}"


# ── картка товару (стек зверху вниз, без накладань) ───────────────────────────
def draw_card(canvas, draw, x, y, w, h, item, *, tag=None):
    rounded(draw, (x + 3, y + 5, x + w + 3, y + h + 5), 26, SHADOW)
    rounded(draw, (x, y, x + w, y + h), 26, CARD)

    pad = 18
    img_sz = w - pad * 2
    ix, iy = x + pad, y + pad

    img = item.get("_img")
    if img is not None:
        m = Image.new("L", (img_sz, img_sz), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, img_sz, img_sz), 18, fill=255)
        canvas.paste(img.resize((img_sz, img_sz)), (ix, iy), m)
    else:
        rounded(draw, (ix, iy, ix + img_sz, iy + img_sz), 18, (236, 237, 242))
        draw.text((ix + img_sz / 2, iy + img_sz / 2), "немає\nфото",
                  font=_f("Arial Bold.ttf", 20), fill=MUTED, anchor="mm",
                  align="center")

    # бейдж знижки — лівий верхній кут фото
    pct = f"-{round(item['discount_pct'])}%"
    bf = _f("Arial Black.ttf", 30)
    bw, bh = draw.textlength(pct, font=bf) + 26, 46
    rounded(draw, (ix + 8, iy + 8, ix + 8 + bw, iy + 8 + bh), bh // 2, RED)
    draw.text((ix + 8 + bw / 2, iy + 8 + bh / 2), pct, font=bf, fill="white", anchor="mm")

    # мітка-гачок — правий нижній кут фото
    if tag:
        tf = _f("Arial Black.ttf", 17)
        tw, th = draw.textlength(tag, font=tf) + 22, 34
        tx2, ty2 = ix + img_sz - tw - 8, iy + img_sz - th - 8
        rounded(draw, (tx2, ty2, tx2 + tw, ty2 + th), th // 2, ACCENT)
        draw.text((tx2 + tw / 2, ty2 + th / 2), tag, font=tf, fill="white", anchor="mm")

    # мережа — білий бейдж у правому верхньому куті фото (звільняє рядок ціни)
    cl = CHAIN_LABEL.get(item["chain"], item["chain"])
    cf = _f("Arial Bold.ttf", 17)
    clw = draw.textlength(cl, font=cf) + 20
    cbx = ix + img_sz - clw - 8
    rounded(draw, (cbx, iy + 8, cbx + clw, iy + 8 + 30), 15, (255, 255, 255))
    draw.text((cbx + clw / 2, iy + 8 + 15), cl, font=cf, fill=(86, 90, 100), anchor="mm")

    # назва — фіксовано 2 рядки (вирівнює ціни у всіх картках)
    nf = _f("Arial Bold.ttf", 23)
    ny = iy + img_sz + 16
    lines = wrap2(draw, " ".join(item["title"].split()), nf, img_sz)
    for i, ln in enumerate(lines):
        draw.text((ix, ny + i * (nf.size + 6)), ln, font=nf, fill=INK)

    # ціна: нова (Arial Black, зелене) + «грн»; праворуч стек: -XX% НАД старою
    # закресленою ціною
    py = ny + 2 * (nf.size + 6) + 8
    gf = _f("Arial Bold.ttf", 22)
    of = _f("Arial.ttf", 21)
    pf2 = _f("Arial Black.ttf", 19)
    num, old = money(item["price"]), money(item["old_price"])
    pct = f"-{round(item['discount_pct'])}%"
    gw = draw.textlength(" грн", font=gf)
    rblock = max(draw.textlength(old, font=of), draw.textlength(pct, font=pf2))
    psize = 40
    while psize > 24:
        pf = _f("Arial Black.ttf", psize)
        if draw.textlength(num, font=pf) + gw + 16 + rblock <= img_sz:
            break
        psize -= 2
    pf = _f("Arial Black.ttf", psize)
    nw = draw.textlength(num, font=pf)
    base = py + (40 - psize)              # спільна базова для різних розмірів
    draw.text((ix, base), num, font=pf, fill=GREEN)
    draw.text((ix + nw, base + (psize - gf.size) - 2), " грн", font=gf, fill=GREEN)
    rx = ix + nw + gw + 16
    draw.text((rx, base - 1), pct, font=pf2, fill=RED)        # відсоток зверху
    oy = base + pf2.size + 3
    draw.text((rx, oy), old, font=of, fill=MUTED)             # стара ціна під ним
    ow = draw.textlength(old, font=of)
    ly = oy + of.size / 2 + 1
    draw.line((rx - 2, ly, rx + ow + 2, ly), fill=RED, width=3)


CARD_H = 446   # pad18 + img(≈276) + 16 + назва56 + 8 + ціна46 + pad18

# ── один банер (сітка 2×3) ────────────────────────────────────────────────────
def render_sheet(items, subtitle, out_path, *, hook_idx=None):
    W, H = 1080, 1350
    M, gap, cols = 48, 24, 3
    canvas = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(canvas)

    # шапка
    tx = M
    try:
        logo = ImageOps.contain(
            Image.open("public/static/logo.png").convert("RGBA"), (104, 104))
        canvas.paste(logo, (M, 44), logo)
        tx = M + 120
    except Exception:
        pass
    draw.text((tx, 46), "ТОП ЗНИЖКИ", font=_f("Arial Black.ttf", 56), fill=INK)
    draw.text((tx, 114), subtitle, font=_f("Arial Bold.ttf", 27), fill=ACCENT)

    # сітка 2×3
    cw = (W - 2 * M - gap * (cols - 1)) // cols
    top = 196
    for i, it in enumerate(items[:6]):
        cx = M + (i % cols) * (cw + gap)
        cy = top + (i // cols) * (CARD_H + gap)
        tag = "ВЕЛИКА ПОКУПКА" if hook_idx is not None and i == hook_idx else None
        draw_card(canvas, draw, cx, cy, cw, CARD_H, it, tag=tag)

    # футер: «Усі знижки щодня —  [tg] @sales_ua_bot» (іконка перед хендлом)
    fy = H - 96
    cy = fy + 32
    rounded(draw, (M, fy, W - M, fy + 64), 32, INK)
    ff = _f("Arial Black.ttf", 32)
    prefix, handle = "Усі знижки щодня —  ", "@sales_ua_bot"
    ico = telegram_icon(42)
    g = 8                                              # відступ між іконкою та хендлом
    wp = draw.textlength(prefix, font=ff)
    wh = draw.textlength(handle, font=ff)
    total = wp + ico.width + g + wh
    x = (W - total) / 2
    draw.text((x, cy), prefix, font=ff, fill="white", anchor="lm")
    canvas.paste(ico, (int(x + wp), int(cy - ico.height / 2)), ico)
    draw.text((x + wp + ico.width + g, cy), handle, font=ff, fill="white", anchor="lm")

    canvas.save(out_path, "PNG")
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────
def _log(tag, rows):
    print(f"\n[{tag}]")
    for r in rows:
        print(f"  -{round(r['discount_pct']):>3}%  "
              f"{money(r['price']):>8}<-{money(r['old_price']):>8}  "
              f"{r['chain']:<7} {r['title'][:46]}")


def main():
    ap = argparse.ArgumentParser(description="Промо-банери з топ-знижками")
    ap.add_argument("--db", default="discounts.db")
    ap.add_argument("--out", default=None,
                    help="база імені (без .png); типово best-sales-images/<дата>/promo")
    ap.add_argument("--min-price", type=float, default=15.0,
                    help="мін. поточна ціна для food-топів (відсікти дріб'язок)")
    ap.add_argument("--pct-lo", type=float, default=30.0)
    ap.add_argument("--pct-hi", type=float, default=80.0,
                    help="стеля % — вище зазвичай накрутка/неліквід")
    args = ap.parse_args()
    if args.out:
        base = args.out[:-4] if args.out.endswith(".png") else args.out
    else:
        base = os.path.join("best-sales-images", f"{date.today():%Y-%m-%d}", "promo")
    os.makedirs(os.path.dirname(base) or ".", exist_ok=True)

    db = sqlite3.connect(args.db)
    ensure_indexes(db)

    pct = fetch_top(db, idx=COVER_DISC, order_expr="discount_pct",
                    where="and discount_pct between ? and ? and price>=? and old_price<?",
                    params=[args.pct_lo, args.pct_hi, args.min_price, 2000],
                    cats=FOOD, want=6)
    save = fetch_top(db, idx=COVER_SAVE, order_expr="(old_price-price)",
                     where="and price>=? and old_price<? and discount_pct between ? and ?",
                     params=[args.min_price, 1500, args.pct_lo, 99],
                     cats=FOOD_SAVE, want=5)
    hook = fetch_top(db, idx=COVER_SAVE, order_expr="(old_price-price)",
                     where="and price>=? and old_price<? and discount_pct between ? and ?",
                     params=[200, 100000, 25, 90],
                     cats=HOOK, want=1)
    hook_item = hook[0] if hook else None

    if len(pct) < 6 or len(save) < 5:
        print("Недостатньо даних під фільтри — перевір БД/категорії", file=sys.stderr)
        sys.exit(1)

    _log("ТОП %", pct)
    _log("ЕКОНОМІЯ грн", save)
    if hook_item:
        _log("ГАЧОК (велика покупка)", [hook_item])

    print("\nзавантаження фото…")
    prefetch(pct, 300)
    save_combo = save + ([hook_item] if hook_item else [])
    prefetch(save_combo, 300)

    d = f"{date.today():%d.%m.%Y}"
    p1 = render_sheet(pct, f"Найвигідніші % · {d}", f"{base}_pct.png")
    p2 = render_sheet(save_combo, f"Найбільша економія · {d}", f"{base}_save.png",
                      hook_idx=(len(save_combo) - 1 if hook_item else None))
    print(f"\n✓ збережено: {p1}\n✓ збережено: {p2}")


if __name__ == "__main__":
    main()
