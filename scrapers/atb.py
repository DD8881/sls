import html as _html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as creq

from scrapers.base import BaseScraper, ScrapedCategory, ScrapedProduct, StoreInfo
from scrapers.http import REQUEST_GATE

log = logging.getLogger(__name__)

BASE = "https://www.atbmarket.com"
IMPERSONATE = "chrome"  # TLS fingerprint that passes ATB's Cloudflare challenge

# ATB promo prices are national (single weekly flyer for all of Ukraine),
# so we scrape the catalog ONCE and attach the same products to every city.
# Curated list of major cities (ATB is present in ~every Ukrainian city).
# Exact spelling matches the other chains' city strings so they share one city
# page. Replace with real store-locator data for precision.
ATB_CITIES = [
    "Київ", "Харків", "Одеса", "Дніпро", "Львів", "Запоріжжя", "Кривий Ріг",
    "Миколаїв", "Вінниця", "Полтава", "Чернігів", "Черкаси", "Житомир", "Суми",
    "Хмельницький", "Рівне", "Івано-Франківськ", "Тернопіль", "Луцьк", "Ужгород",
    "Чернівці", "Кропивницький", "Біла Церква", "Кременчук", "Кам'янське",
    "Бровари", "Бориспіль", "Ірпінь",
]

CAT_WORKERS = 6   # parallel categories
PAGE_WORKERS = 4  # parallel pages within a category
PAGE_SIZE = 36    # ATB returns 36 products per catalog page
RETRIES = 3       # retry on timeout / transient network errors
TIMEOUT = 30

# --- regexes over the catalog HTML ---
_CARD_RE = re.compile(r'<article class="\s*catalog-item.*?</article>', re.S)
_SALE_MARKER = "product-price--sale"
_ID_RE = re.compile(r'wishlist\?id=(\d+)')
_URL_RE = re.compile(r'href="(/product/[^"]+)"')
_ALT_RE = re.compile(r'<img[^>]+alt="Купити\s+(.*?)\s+у АТБ Market"')
_WEBP_RE = re.compile(r'srcset="(https://src\.zakaz\.atbmarket\.com/[^"]+\.webp)"')
_JPG_RE = re.compile(r'<img[^>]+src="(https://src\.zakaz\.atbmarket\.com/[^"]+)"')
_TOP_RE = re.compile(r'<data value="([\d.]+)" class="product-price__top"')
_BOTTOM_RE = re.compile(r'<data value="([\d.]+)" class="product-price__bottom"')
_UNIT_RE = re.compile(r'<span class="product-price__unit">(/[^<]+)</span>')
_TOTAL_RE = re.compile(r'Показано\s+\d+\s+з\s+(\d+)')
# Every catalog link in the menu tree (departments + leaf subcategories).
# Top-level departments (Бакалія, Алкоголь...) have NO single parent for dairy,
# so we must scrape the full leaf set to cover everything; products dedupe by id.
_CAT_LINK_RE = re.compile(
    r'href="(?:/catalog/)(\d+-[a-z0-9-]+)"[^>]*>\s*(?:<[^>]+>\s*)*([^<>{}]{2,60}?)\s*<'
)
# Cross-cutting promo collections (not real departments) — skip for classification.
_SKIP_SLUGS = ("388-aktsiya",)
_BAD_TITLES = {">", "Скинути всі фільтри"}


class ATBScraper(BaseScraper):
    def __init__(self):
        self._local = threading.local()
        self._products_cache: list[ScrapedProduct] | None = None
        self._cache_lock = threading.Lock()

    def _session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = creq.Session(impersonate=IMPERSONATE)
            self._local.session = s
        return s

    def _get(self, url: str) -> str:
        last_err = None
        for attempt in range(RETRIES):
            try:
                with REQUEST_GATE:
                    resp = self._session().get(url, timeout=TIMEOUT)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                last_err = e
                if attempt < RETRIES - 1:
                    log.debug("[atb] retry %d/%d %s: %s", attempt + 1, RETRIES, url, e)
                    time.sleep(1.5 * (attempt + 1))
        raise last_err

    def chain_name(self) -> str:
        return "atb"

    def scrape_categories(self) -> list[ScrapedCategory]:
        log.info("[atb] Fetching categories...")
        page = self._get(f"{BASE}/")
        seen, cats = set(), []
        for slug, title in _CAT_LINK_RE.findall(page):
            if slug in seen or slug.startswith(_SKIP_SLUGS):
                continue
            title = _html.unescape(re.sub(r"\s+", " ", title)).strip()
            if not title or title in _BAD_TITLES:
                continue
            seen.add(slug)
            cats.append(ScrapedCategory(chain="atb", slug=slug, title=title, parent_slug=None))
        log.info("[atb] Found %d categories", len(cats))
        return cats

    def get_stores(self) -> list[StoreInfo]:
        # Synthetic per-city stores sharing the national promo catalog.
        return [
            StoreInfo(
                id=f"atb_{_translit(city)}",
                chain="atb",
                name=f"АТБ ({city})",
                city=city,
                address=None,
            )
            for city in ATB_CITIES
        ]

    def scrape_products(self, store: StoreInfo) -> list[ScrapedProduct]:
        # National catalog is identical for every store — scrape once, cache.
        with self._cache_lock:
            if self._products_cache is None:
                self._products_cache = self._scrape_all()
        return self._products_cache

    def _scrape_all(self) -> list[ScrapedProduct]:
        categories = self.scrape_categories()
        products: dict[str, ScrapedProduct] = {}
        with ThreadPoolExecutor(max_workers=CAT_WORKERS) as pool:
            futures = {pool.submit(self._scrape_category, c.slug): c.slug for c in categories}
            for fut in as_completed(futures):
                slug = futures[fut]
                try:
                    for p in fut.result():
                        products[p.external_id] = p  # dedupe across categories
                except Exception as e:
                    log.warning("[atb] category %s failed: %s", slug, e)
        log.info("[atb] %d unique promo products", len(products))
        return list(products.values())

    def _scrape_category(self, slug: str) -> list[ScrapedProduct]:
        first = self._get(f"{BASE}/catalog/{slug}")
        items = self._parse_sale_cards(first, slug)
        m = _TOTAL_RE.search(first)
        total = int(m.group(1)) if m else len(items)
        if total <= PAGE_SIZE:
            return items

        pages = range(2, (total // PAGE_SIZE) + 2)
        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            futures = [pool.submit(self._get, f"{BASE}/catalog/{slug}?page={p}") for p in pages]
            for fut in as_completed(futures):
                try:
                    items.extend(self._parse_sale_cards(fut.result(), slug))
                except Exception as e:
                    log.warning("[atb] %s page failed: %s", slug, e)
        return items

    def _parse_sale_cards(self, html: str, slug: str) -> list[ScrapedProduct]:
        out = []
        for card in _CARD_RE.findall(html):
            if _SALE_MARKER not in card:
                continue
            top = _TOP_RE.search(card)
            bottom = _BOTTOM_RE.search(card)
            if not top:
                continue
            price = float(top.group(1))
            old_price = float(bottom.group(1)) if bottom else None
            discount_pct = None
            if old_price and old_price > 0 and price < old_price:
                discount_pct = round((1 - price / old_price) * 100)

            pid = _ID_RE.search(card)
            url = _URL_RE.search(card)
            alt = _ALT_RE.search(card)
            webp = _WEBP_RE.search(card)
            jpg = _JPG_RE.search(card)
            unit = _UNIT_RE.search(card)
            if not pid or not url:
                continue

            out.append(ScrapedProduct(
                external_id=f"atb_{pid.group(1)}",
                chain="atb",
                category_slug=slug,
                title=(alt.group(1).strip() if alt else url.group(1).rsplit("/", 1)[-1]),
                price=price,
                old_price=old_price,
                discount_pct=discount_pct,
                image_url=(webp.group(1) if webp else (jpg.group(1) if jpg else None)),
                url=f"{BASE}{url.group(1)}",
                unit=(unit.group(1).lstrip("/") if unit else None),
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
    s = ATBScraper()
    prods = s.scrape_products(s.get_stores()[0])
    print(f"\n{len(prods)} promo products")
    for p in prods[:8]:
        print(f"  {p.discount_pct}% | {p.price}→{p.old_price} грн | {p.title[:50]} | {p.category_slug}")
