from __future__ import annotations

import html as _html
import logging
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from scrapers.base import BaseScraper, ScrapedCategory, ScrapedProduct, StoreInfo

log = logging.getLogger(__name__)

BASE = "https://fozzyshop.ua"
# Per-department sale listing: /{id-slug}/promo=akciyni-propozicii?ajax=1
# wraps the rendered listing HTML in JSON (Fozzy is a jQuery SSR site, no
# structured product JSON — same situation as Novus/ATB).
PROMO_PATH = "promo=akciyni-propozicii"
ZAGLUSHKA = "zaglushka"  # placeholder image when a product has no photo

# Fozzy is an online-delivery chain. Coverage = delivery zones (uncertain);
# listed in major cities to match user expectation — verify real delivery areas.
FOZZY_CITIES = ["Київ", "Дніпро", "Харків", "Одеса", "Львів", "Запоріжжя"]

PAGE_SIZE = 24    # fixed server-side; per_page/limit params are ignored
CAT_WORKERS = 6   # parallel departments
PAGE_WORKERS = 4  # parallel pages within a department

# Real top-level departments (canonical route slugs). Cross-cutting promo
# collections (onlayn-deshevshe, lito-piknik, festyval-bagazhu, rozprodazh,
# svyatkovi-sezonni) are intentionally skipped — their products already live in
# a real department, and dedupe by external_id covers the overlap.
CATEGORIES = [
    ("3643-kulinariya", "Кулінарія"),
    ("3644-solodoshchi-sneky", "Солодощі, снеки"),
    ("3645-tovary-dlya-ditey", "Товари для дітей"),
    ("3646-sousy-spetsiyi-roslynna-oliya", "Соуси, спеції, рослинна олія"),
    ("3647-m-yaso-ptytsya", "М'ясо, птиця"),
    ("3648-odyag-vzuttya-aksesuary", "Одяг, взуття, аксесуари"),
    ("3649-pobutova-tekhnika", "Побутова техніка"),
    ("3650-molochna-produktsiya-syr-yaytsya", "Молочна продукція, сир, яйця"),
    ("3651-bezalkogolni-napoyi", "Безалкогольні напої"),
    ("3652-tovary-dlya-domu-avto", "Товари для дому, авто"),
    ("3654-alkogol", "Алкоголь"),
    ("3655-zootovary", "Зоотовари"),
    ("3656-zdorove-i-sportyvne-kharchuvannya", "Здорове і спортивне харчування"),
    ("3657-kosherni-produkty", "Кошерні продукти"),
    ("3660-chay-kava", "Чай, кава"),
    ("3661-zamorozheni-produkty", "Заморожені продукти"),
    ("3663-krasa-gigiyena", "Краса, гігієна"),
    ("3664-khlibobulochni-vyroby", "Хлібобулочні вироби"),
    ("3665-kovbasa-m-yasni-vyroby", "Ковбаса, м'ясні вироби"),
    ("3666-ryba-moreprodukty", "Риба, морепродукти"),
    ("3667-bakaliya-konservatsiya", "Бакалія, консервація"),
    ("3668-ovochi-frukty-gryby", "Овочі, фрукти, гриби"),
    ("3669-tovary-dlya-dachi-vidpochynku", "Товари для дачі, відпочинку"),
    ("3670-kantstovary-knygy-presa", "Канцтовари, книги, преса"),
    ("3708-aziatska-kukhnya", "Азіатська кухня"),
    ("3814-pobutova-khimiya", "Побутова хімія"),
]

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE}/sales",
}

# --- regexes over the per-product listing HTML ---
# The listing repeats one <div class="product_mini_outer"> per product, so we
# split on that boundary and parse each chunk independently (no balanced-div matching).
_OUTER_SPLIT = re.compile(r'class="product_mini_outer"')
_ID_RE = re.compile(r'data-product-id="(\d+)"')
_NAME_RE = re.compile(r'data-product-name="([^"]*)"')
_MAIN_PRICE_RE = re.compile(r'data-main-price="([\d.]+)"')
_OLD_PRICE_RE = re.compile(r'data-secondary-price="([\d.]+)"')
_DISCOUNT_RE = re.compile(r'data-discount-from-old-price="-?(\d+)%"')
_UNIT_RE = re.compile(r'<div class="product_mini_unit">\s*<span>([^<]*)</span>')
_IMG_RE = re.compile(r'<img[^>]+src="(https://media\.fozzyshop\.ua/[^"]+)"')
_URL_RE = re.compile(r'href="(https://fozzyshop\.ua/[^"]+\.html)"')
_COUNT_RE = re.compile(r'\(([\d\s ]+)\s*товар')


class FozzyScraper(BaseScraper):
    def __init__(self):
        self._local = threading.local()
        self._products_cache: list[ScrapedProduct] | None = None
        self._cache_lock = threading.Lock()

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            self._local.session = s
        return s

    def chain_name(self) -> str:
        return "fozzy"

    def get_stores(self) -> list[StoreInfo]:
        # Synthetic per-city store(s) sharing the single online catalog.
        return [
            StoreInfo(
                id=f"fozzy_{_translit(city)}",
                chain="fozzy",
                name=f"Fozzy ({city})",
                city=city,
                address=None,
            )
            for city in FOZZY_CITIES
        ]

    def scrape_categories(self) -> list[ScrapedCategory]:
        return [
            ScrapedCategory(chain="fozzy", slug=slug, title=title, parent_slug=None)
            for slug, title in CATEGORIES
        ]

    def scrape_products(self, store: StoreInfo) -> list[ScrapedProduct]:
        # One online catalog for all (Kyiv) stores — scrape once, cache.
        with self._cache_lock:
            if self._products_cache is None:
                self._products_cache = self._scrape_all()
        return self._products_cache

    def _scrape_all(self) -> list[ScrapedProduct]:
        log.info("[fozzy] Scraping %d departments...", len(CATEGORIES))
        products: dict[str, ScrapedProduct] = {}
        with ThreadPoolExecutor(max_workers=CAT_WORKERS) as pool:
            futures = {pool.submit(self._scrape_category, slug): slug for slug, _ in CATEGORIES}
            for fut in as_completed(futures):
                slug = futures[fut]
                try:
                    for p in fut.result():
                        products[p.external_id] = p  # dedupe across departments
                except Exception as e:
                    log.warning("[fozzy] department %s failed: %s", slug, e)
        log.info("[fozzy] %d unique sale products", len(products))
        return list(products.values())

    def _scrape_category(self, slug: str) -> list[ScrapedProduct]:
        first = self._fetch_json(slug, 1)
        listing = (first.get("data") or {}).get("products_and_filters", "")
        total = self._parse_total(first)
        items = self._parse_cards(listing, slug)

        total_pages = math.ceil(total / PAGE_SIZE) if total else 1
        if total_pages <= 1:
            return items

        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            futures = {pool.submit(self._fetch_listing, slug, pg): pg for pg in range(2, total_pages + 1)}
            for fut in as_completed(futures):
                pg = futures[fut]
                try:
                    items.extend(self._parse_cards(fut.result(), slug))
                except Exception as e:
                    log.warning("[fozzy] %s page %d failed: %s", slug, pg, e)
        return items

    def _url(self, slug: str, page: int) -> str:
        # The site dropped the ?ajax=1 param (it now 302-redirects). The XHR
        # header alone yields JSON. Page 1 must omit ?page (page=1 also 302s);
        # pages 2..N use ?page=N.
        base = f"{BASE}/{slug}/{PROMO_PATH}"
        return base if page <= 1 else f"{base}?page={page}"

    def _fetch_json(self, slug: str, page: int) -> dict:
        resp = self._session().get(self._url(slug, page), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _fetch_listing(self, slug: str, page: int) -> str:
        return (self._fetch_json(slug, page).get("data") or {}).get("products_and_filters", "")

    @staticmethod
    def _parse_total(payload: dict) -> int:
        raw = (payload.get("data") or {}).get("products_count", "")
        m = _COUNT_RE.search(raw)
        return int(re.sub(r"\D", "", m.group(1))) if m else 0

    def _parse_cards(self, listing: str, category_slug: str) -> list[ScrapedProduct]:
        out = []
        for chunk in _OUTER_SPLIT.split(listing)[1:]:  # [0] is markup before the first card
            pid = _ID_RE.search(chunk)
            main = _MAIN_PRICE_RE.search(chunk)
            url = _URL_RE.search(chunk)
            if not pid or not main or not url:
                continue

            price = float(main.group(1))
            old_m = _OLD_PRICE_RE.search(chunk)
            old_price = float(old_m.group(1)) if old_m else None
            # require a real markdown to be safe
            if not old_price or old_price <= price:
                continue

            disc_m = _DISCOUNT_RE.search(chunk)
            discount_pct = float(disc_m.group(1)) if disc_m else round((1 - price / old_price) * 100, 1)

            name_m = _NAME_RE.search(chunk)
            unit_m = _UNIT_RE.search(chunk)
            img_m = _IMG_RE.search(chunk)
            image_url = img_m.group(1) if img_m and ZAGLUSHKA not in img_m.group(1) else None

            out.append(ScrapedProduct(
                external_id=f"fozzy_{pid.group(1)}",
                chain="fozzy",
                category_slug=category_slug,
                title=_html.unescape(name_m.group(1)).strip() if name_m else "",
                price=price,
                old_price=old_price,
                discount_pct=discount_pct,
                image_url=image_url,
                url=url.group(1),
                unit=_html.unescape(unit_m.group(1)).strip() if unit_m else None,
                in_stock=True,
                promo_end_date=None,
            ))
        return out


def _translit(city: str) -> str:
    table = {"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
             "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i",
             "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
             "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
             "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia"}
    return "".join(table.get(ch, ch) for ch in city.lower())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = FozzyScraper()
    prods = s.scrape_products(s.get_stores()[0])
    print(f"\n{len(prods)} promo products")
    for p in prods[:8]:
        print(f"  {p.discount_pct}% | {p.price}→{p.old_price} грн | {p.title[:45]} | {p.category_slug}")
